"""诊断信息：接口字段尚未用真实账号全量确认，脱敏后导出原始结构方便对照。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_COOKIES, CONF_CUSTNUMBER, CONF_PHONE, CONF_TOKEN
from .coordinator import NanwangConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    CONF_COOKIES,
    CONF_PHONE,
    CONF_CUSTNUMBER,
    "phoneNumber",
    "mobile",
    "userName",
    "custName",
    "eleCustName",
    "eleAddress",
    "address",
    "eleCustNumber",
    "eleCustId",
    "bindingId",
    "meteringPointId",
    "meteringPointNumber",
    "settleAcctNumber",
    "deviceIdentif",
    "idCard",
    "certNo",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NanwangConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "accounts": [
            async_redact_data(asdict(acc), TO_REDACT)
            for acc in (coordinator.data or {}).values()
        ],
    }
