"""配置流程测试。"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.nanwang.api import NanwangApiError, NanwangAuthError
from custom_components.nanwang.const import DOMAIN


async def _start(hass: HomeAssistant, method: str) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": method})


async def test_sms_flow(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    result = await _start(hass, "sms")
    assert result["step_id"] == "sms"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "12345", "area_code": "030000"}
    )
    assert result["errors"] == {"phone": "invalid_phone"}
    mock_api["async_send_sms"].assert_not_called()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "13800001234", "area_code": "090000"}
    )
    assert result["step_id"] == "sms_code"
    assert result["description_placeholders"]["phone"] == "138****1234"
    mock_api["async_send_sms"].assert_awaited_once_with("13800001234", "090000")

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "南网 138****1234"
    assert result["result"].unique_id == "CUST1"
    assert result["data"]["token"] == "tok-login"
    assert result["data"]["phone"] == "13800001234"
    assert result["data"]["area_code"] == "090000"


async def test_sms_errors(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    mock_api["async_send_sms"].side_effect = NanwangApiError("0009", "频繁", "/center/sendMsg")
    result = await _start(hass, "sms")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "13800001234", "area_code": "030000"}
    )
    assert result["errors"] == {"base": "sms_failed"}
    assert "明天再试" in result["description_placeholders"]["message"]

    mock_api["async_send_sms"].side_effect = None
    mock_api["async_login_sms"].side_effect = NanwangApiError("0001003", "", "/center/login")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "13800001234", "area_code": "030000"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["step_id"] == "sms_code"
    assert result["errors"] == {"base": "login_failed"}


async def test_token_flow(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    result = await _start(hass, "token")
    mock_api["async_get_bound_users"].side_effect = NanwangAuthError("04", None, "/x")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "bad", "area_code": "030000"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api["async_get_bound_users"].side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": " tok-browser ", "custnumber": "C9", "area_code": "030000"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token"] == "tok-browser"
    assert result["result"].unique_id == "C9"


async def test_no_accounts(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    mock_api["async_get_bound_users"].return_value = []
    result = await _start(hass, "token")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "t", "area_code": "030000"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_accounts"


async def test_reauth(hass: HomeAssistant, mock_api: dict[str, AsyncMock]) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="CUST1",
        data={"token": "old", "phone": "13800001234", "area_code": "030000", "auth_method": "sms"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "token"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "new", "area_code": "030000"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()  # 等后台 reload 结束
    assert entry.data["token"] == "new"
    assert entry.data["phone"] == "13800001234"


@pytest.mark.parametrize(("options", "current_hours"), [({}, 4), ({"update_interval_hours": 12}, 12)])
async def test_refresh_options_apply_without_restart(
    hass: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
    options: dict[str, int],
    current_hours: int,
) -> None:
    freezer.move_to("2026-11-16 00:00:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="CUST1", data={"token": "tok"}, options=options
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({})["update_interval_hours"] == current_hours
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"update_interval_hours": 6}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"update_interval_hours": 6}
    assert entry.data == {"token": "tok"}
    assert entry.runtime_data.update_interval == timedelta(hours=6)

    calls = mock_api["async_get_bound_users"]
    assert calls.await_count == 2  # 初次加载和配置保存后各读取一次
    freezer.tick(timedelta(hours=4, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calls.await_count == 2
    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calls.await_count == 3


@pytest.mark.parametrize("hours", [0, 25, 1.5])
async def test_refresh_options_reject_invalid_interval(hass: HomeAssistant, hours: float) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "tok"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    if hours in (0, 25):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {"update_interval_hours": hours}
            )
    else:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"update_interval_hours": hours}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"update_interval_hours": "invalid_interval"}
    assert entry.options == {}

