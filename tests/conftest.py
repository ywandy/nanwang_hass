"""测试公共夹具。数据形状对齐 2026-11-12 真实账号实测（findings/poc-verification.md §6）。"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.nanwang.api import NanwangClient

USER = {
    "eleCustNumber": "0300001234567890",
    "bindingId": "B1",
    "areaCode": "080000",
    "eleType": "500",
    "supplyUnit": "08001210",
    "totalOwnedFee": "0",
}
POINT = {"meteringPointId": "MPID", "meteringPointNumber": "81***17387386", "areaCode": "080000"}
SURPLUS = [{"meteringPointNumber": "81***17387386", "balance": "88.80", "arrears": "0", "lockedBalance": "1.20"}]
YEARLY = {
    "totalBillingElectricity": "1234.5",
    "totalActualAmount": "765.43",
    "electricAndChargeList": [
        {"yearMonth": "2026-09", "billingElectricity": "300", "actualTotalAmount": "180.1"},
        {"yearMonth": "2026-10", "billingElectricity": "250", "actualTotalAmount": "150.25"},
        {"yearMonth": "2026-11", "billingElectricity": None, "actualTotalAmount": None},
    ],
}


def daily_for(year_month: str, day2: str = "12.5") -> dict[str, Any]:
    """某月的逐日数据：1 号 10 kWh、2 号 day2 kWh、3 号还没出。"""
    ym = f"{year_month[:4]}-{year_month[4:]}"
    return {
        "totalPower": "40.5",
        "result": [
            {"date": f"{ym}-01", "power": "10.0", "maxTemperature": "30.5", "minTemperature": "21"},
            {"date": f"{ym}-02", "power": day2, "maxTemperature": "29", "minTemperature": "20"},
            {"date": f"{ym}-03", "power": None},
        ],
    }


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """允许加载 custom_components/。"""


@pytest.fixture
def mock_api() -> Generator[dict[str, AsyncMock]]:
    """把 NanwangClient 的网络方法换成固定数据。"""
    mocks = {
        "async_send_sms": AsyncMock(return_value=None),
        "async_login_sms": AsyncMock(),
        "async_get_bound_users": AsyncMock(return_value=[USER]),
        "async_get_metering_points": AsyncMock(return_value=[POINT]),
        "async_get_account_surplus": AsyncMock(return_value=SURPLUS),
        "async_get_yearly": AsyncMock(return_value=YEARLY),
        "async_get_daily": AsyncMock(side_effect=lambda user, point, ym: daily_for(ym)),
    }

    async def _login(self: NanwangClient, phone: str, code: str, area: str) -> str:
        self.token, self.custnumber = "tok-login", "CUST1"
        return self.token

    others = {k: v for k, v in mocks.items() if k != "async_login_sms"}
    with (
        patch.multiple(NanwangClient, **others),
        patch.object(NanwangClient, "async_login_sms", autospec=True, side_effect=_login) as login,
    ):
        mocks["async_login_sms"] = login
        yield mocks
