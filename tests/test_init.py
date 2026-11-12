"""集成加载、传感器、token 回写、重新认证、诊断。"""

from __future__ import annotations

import json
import logging
import pathlib
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nanwang.api import NanwangApiError, NanwangAuthError
from custom_components.nanwang.const import DOMAIN
from custom_components.nanwang.diagnostics import async_get_config_entry_diagnostics

from .conftest import USER

NUMBER = USER["eleCustNumber"]


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="CUST1",
        data={"token": "tok", "custnumber": "CUST1", "phone": "13800001234", "area_code": "030000"},
    )
    entry.add_to_hass(hass)
    return entry


def _state(hass: HomeAssistant, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{NUMBER}_{key}")
    assert entity_id, key
    return hass.states.get(entity_id)


@pytest.fixture
async def shanghai(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Asia/Shanghai")
    freezer.move_to("2026-11-12 12:00:00+08:00")


@pytest.mark.usefixtures("shanghai")
async def test_sensors(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert _state(hass, "balance").state == "88.8"
    assert _state(hass, "arrears").state == "0.0"
    assert _state(hass, "locked_balance").state == "1.2"
    assert _state(hass, "year_energy").state == "1234.5"
    assert _state(hass, "year_cost").attributes["unit_of_measurement"] == "CNY"

    last = _state(hass, "last_bill_energy")
    assert last.state == "250.0"
    assert last.attributes["year_month"] == "2026-10"
    assert float(_state(hass, "unit_price").state) == pytest.approx(0.601)

    month = _state(hass, "month_energy")
    assert month.state == "40.5"
    assert month.attributes["state_class"] == "total"
    assert month.attributes["month"] == "2026-11"
    assert "daily" not in month.attributes
    estimated = _state(hass, "month_cost_estimated")
    assert float(estimated.state) == pytest.approx(40.5 * 0.601)
    assert estimated.attributes["price_from"] == "2026-10"
    assert estimated.attributes["unit_price"] == 0.601

    day = _state(hass, "latest_day_energy")
    assert day.state == "12.5"
    assert day.attributes["date"] == "2026-11-02"
    # 只留实时值：不再暴露逐日列表和气温
    assert not {"recent_days", "max_temperature", "min_temperature"} & set(day.attributes)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, NUMBER)})
    assert device.model == "广州 · 居民"

    # 没有 recorder 时不导统计；每轮查当月 + 15 天窗口跨到的上月；计量点缓存，不重复查
    await entry.runtime_data.async_refresh()
    assert mock_api["async_get_metering_points"].await_count == 1
    assert [c.args[2] for c in mock_api["async_get_daily"].await_args_list] == [
        "202611", "202610", "202611", "202610"
    ]


async def test_recent_days_cross_month(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_api: dict[str, AsyncMock]
) -> None:
    await hass.config.async_set_time_zone("Asia/Shanghai")
    freezer.move_to("2026-11-03 10:00:00+08:00")

    def _daily(user, point, ym):
        days = range(1, 3) if ym == "202611" else range(1, 32)
        return {"result": [
            {"date": f"{ym[:4]}-{ym[4:]}-{d:02d}", "power": "1.0"} for d in days
        ]}

    mock_api["async_get_daily"].side_effect = _daily
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # 月初：最近一日要能跨月找；内部的 15 天窗口从 10-19 开始
    dates = [d["date"] for d in entry.runtime_data.data[NUMBER].recent_days]
    assert dates[0] == "2026-10-19" and dates[-1] == "2026-11-02"
    assert len(dates) == 15
    assert _state(hass, "latest_day_energy").attributes["date"] == "2026-11-02"


async def test_fixed_refresh_schedule(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_api: dict[str, AsyncMock]
) -> None:
    """只在每天 0 点、12 点刷新。"""
    await hass.config.async_set_time_zone("Asia/Shanghai")
    freezer.move_to("2026-11-12 10:00:00+08:00")
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    calls = mock_api["async_get_bound_users"]
    assert calls.await_count == 1

    for moment, expected in (
        ("2026-11-12 11:59:59+08:00", 1),
        ("2026-11-12 12:00:00+08:00", 2),
        ("2026-11-12 18:00:00+08:00", 2),
        ("2026-11-13 00:00:00+08:00", 3),
    ):
        freezer.move_to(moment)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert calls.await_count == expected, moment


@pytest.mark.usefixtures("shanghai")
async def test_account_without_data(
    hass: HomeAssistant, mock_api: dict[str, AsyncMock], caplog: pytest.LogCaptureFixture
) -> None:
    """充电桩户号：年度/日数据都是 sta=09，传感器为 unknown，不刷警告。"""
    no_data = NanwangApiError("09", "无此用户数据", "/charge/queryDayElectricByMPoint")
    mock_api["async_get_daily"].side_effect = no_data
    mock_api["async_get_yearly"].side_effect = no_data
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "month_energy").state == "unknown"
    assert _state(hass, "balance").state == "88.8"
    assert not [
        r for r in caplog.records
        if r.name.startswith("custom_components.nanwang") and r.levelno >= logging.WARNING
    ]


async def test_token_refresh_persisted(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data.client
    client.token = "tok-new"
    client.on_auth_update()
    assert entry.data["token"] == "tok-new"
    assert entry.data["phone"] == "13800001234"
    assert entry.state is ConfigEntryState.LOADED


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["async_get_bound_users"].side_effect = NanwangAuthError("04", None, "/x")
    entry = _entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_diagnostics_redacted(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in ("tok", "13800001234", NUMBER, "CUST1"):
        assert f"'{secret}'" not in text
    assert diag["accounts"][0]["yearly"]["totalBillingElectricity"] == "1234.5"


# HA 自带、前端自己会翻译的属性
STANDARD_ATTRS = {"friendly_name", "unit_of_measurement", "device_class", "state_class", "last_reset"}


@pytest.mark.usefixtures("shanghai")
async def test_attribute_names_translated(
    hass: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """实体详情里的每个自定义属性都要有翻译，否则前端会显示成英文 key。

    只查 en：其它语言缺的 key 会被 HA 用 en 兜底，中文是否齐全由下面的文件对比保证。
    """
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    translations = await async_get_translations(hass, "en", "entity", {DOMAIN})
    registry = er.async_get(hass)
    missing = []
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        for attr in set(hass.states.get(reg.entity_id).attributes) - STANDARD_ATTRS:
            key = f"component.{DOMAIN}.entity.sensor.{reg.translation_key}.state_attributes.{attr}.name"
            if key not in translations:
                missing.append(key)
    assert not missing


def _keys(obj: dict, prefix: str = "") -> set[str]:
    out = set()
    for k, v in obj.items():
        out |= _keys(v, f"{prefix}{k}.") if isinstance(v, dict) else {f"{prefix}{k}"}
    return out


def test_translation_files_have_same_keys() -> None:
    folder = pathlib.Path(__file__).parents[1] / "custom_components" / DOMAIN / "translations"
    zh = _keys(json.loads((folder / "zh-Hans.json").read_text(encoding="utf-8")))
    en = _keys(json.loads((folder / "en.json").read_text(encoding="utf-8")))
    assert zh == en, {"只在 en": sorted(en - zh), "只在 zh-Hans": sorted(zh - en)}
