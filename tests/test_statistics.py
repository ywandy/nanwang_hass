"""逐日电量导入长期统计（需要 recorder）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nanwang.const import DOMAIN
from custom_components.nanwang.statistics import months_between, sync_window_start

from .conftest import USER, daily_for

STAT_ID = f"{DOMAIN}:{USER['eleCustNumber']}_energy"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: Any
) -> None:
    """recorder_mock 必须先于 hass 建立，覆盖 conftest 里的同名夹具。"""


def _rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    start = datetime(2026, 1, 1, tzinfo=dt_util.UTC)
    return statistics_during_period(
        hass, start, None, {STAT_ID}, "hour", None, {"state", "sum"}
    ).get(STAT_ID, [])


async def test_import_and_rewrite(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_api: dict[str, AsyncMock]
) -> None:
    await hass.config.async_set_time_zone("Asia/Shanghai")
    freezer.move_to("2026-11-12 12:00:00+08:00")
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "t", "area_code": "030000"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await async_wait_recording_done(hass)

    # 首次回填 6 个月（2026-06 ~ 2026-11），每月 2 天有数据
    rows = await hass.async_add_executor_job(_rows, hass)
    assert len(rows) == 12
    first = dt_util.as_local(dt_util.utc_from_timestamp(rows[0]["start"]))
    assert (first.year, first.month, first.day, first.hour) == (2026, 6, 1, 0)
    assert rows[-1]["sum"] == pytest.approx(6 * 22.5)

    # 当月数据被修正：只重写最近 15 天（10-28 起，跨到 10 月），累计值从窗口前一天接上
    mock_api["async_get_daily"].reset_mock()
    mock_api["async_get_daily"].side_effect = lambda user, point, ym: daily_for(
        ym, day2="20.0" if ym == "202611" else "12.5"
    )
    await entry.runtime_data.async_refresh()
    await async_wait_recording_done(hass)

    assert {c.args[2] for c in mock_api["async_get_daily"].await_args_list} == {"202610", "202611"}
    rows = await hass.async_add_executor_job(_rows, hass)
    assert len(rows) == 12
    assert rows[-2]["sum"] == pytest.approx(5 * 22.5 + 10)
    assert rows[-1]["state"] == pytest.approx(20.0)
    assert rows[-1]["sum"] == pytest.approx(5 * 22.5 + 30)


async def test_sync_window(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Asia/Shanghai")
    tz = dt_util.get_default_time_zone()
    now = datetime(2026, 11, 12, 12, tzinfo=tz)

    def window(last: datetime | None) -> tuple[int, int, int]:
        start = sync_window_start(now, last)
        assert (start.hour, start.minute) == (0, 0)
        return start.year, start.month, start.day

    assert window(None) == (2026, 6, 1)  # 首次回填 6 个月
    assert window(datetime(2026, 11, 10, tzinfo=tz)) == (2026, 10, 28)  # 平时：最近 15 天
    assert window(datetime(2026, 10, 2, tzinfo=tz)) == (2026, 10, 2)  # 停机断档：从最后一条补
    assert window(datetime(2024, 1, 1, tzinfo=tz)) == (2025, 12, 1)  # 最多补 12 个月
    assert months_between(datetime(2026, 10, 28, tzinfo=tz), now) == ["202610", "202611"]
