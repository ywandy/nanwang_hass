"""App/mPaaS protocol tests with a local HTTP gateway."""

from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.nanwang.api import (
    P_DAILY,
    P_LOGIN,
    P_METERING,
    P_MONTHLY_BILLS,
    P_SEND_SMS,
    P_USERS,
    NanwangApiError,
    NanwangAuthError,
    NanwangClient,
    decrypt_param,
    encrypt_param,
)

VECTORS = [
    (
        {
            "phoneNumber": "13800000000",
            "credType": "11",
            "vcCode": "123456",
            "areaCode": "030000",
            "defaultAreaCode": "030000",
        },
        "tQB+TLbGQBn6HHBz3nRv2mKpNtmz1kBrsXkplsSeKTp05Cy3snpLuFGYsiLemvCVj+6rbC46DBQOY9ck41r2MkU/p4uRHXKuSF7e+0SrhK73OpEKQdasf8Is/VtUB5RZ9UvsKCVPMARqFWtUYEFfTw==",
    ),
    ({"a": "01234567"}, "klz79h8k2nla4Tw0PoJZ/Q=="),
]

TOKEN = "tok-login"
ROLLED_TOKEN = "tok-refreshed"
USER = {
    "eleCustNumber": "0300001234567890",
    "bindingId": "B1",
    "areaCode": "030000",
    "supplyUnit": "08001210",
}
POINT = {"meteringPointId": "MPID", "meteringPointNumber": "MPNO", "areaCode": "030000"}
BILL = {
    "eleCustNumber": "0300001234567890",
    "electricityBillYearMonth": 202611,
    "receieElectricity": "150.25",
    "totalPower": "250",
}
DAILY = {
    "totalPower": "12.5",
    "result": [{"date": "2026-11-01", "power": "3.2"}],
}


@pytest.mark.parametrize(("obj", "expected"), VECTORS)
def test_app_encrypt_vector(obj: dict[str, Any], expected: str) -> None:
    assert encrypt_param(obj) == expected
    assert json.loads(decrypt_param(expected)) == obj


class Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.auth_sta = "04"

    async def handle(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["path"]
        body = await request.json()
        if request.headers.get("need-crypto") == "true":
            body = json.loads(decrypt_param(body["param"]))
        self.calls.append((path, body, dict(request.headers)))

        if path == P_SEND_SMS:
            if len(body.get("phoneNumber", "")) != 11:
                return web.json_response({"sta": "01", "message": "手机号码格式不正确"})
            return web.json_response({"sta": "00"})

        if path == P_LOGIN:
            if body.get("vcCode") != "123456":
                return web.json_response({"sta": "0001003", "message": "验证码校验失败!"})
            return web.json_response(
                {"sta": "00", "data": None},
                headers={
                    "x-auth-token": TOKEN,
                    "Set-Cookie": "CAMSID=abc; Path=/",
                },
            )

        if request.headers.get("x-auth-token") != TOKEN:
            return web.json_response(
                {"sta": self.auth_sta}, headers={"x-auth-token": "guest"}
            )

        if path == P_USERS:
            return web.json_response(
                {"sta": "00", "data": [USER]},
                headers={"x-auth-token": ROLLED_TOKEN, "Set-Cookie": "SESSION=session; Path=/"},
            )
        if path == P_METERING:
            return web.json_response({"sta": "00", "data": [POINT]})
        if path == P_MONTHLY_BILLS:
            return web.json_response(
                {"sta": "00", "data": {"billUserAndYearMonth": [BILL], "bindsListTotal": 1}}
            )
        if path == P_DAILY:
            return web.json_response(
                {"sta": "00", "data": encrypt_param(DAILY)},
                headers={"need-decrypto": "true"},
            )
        return web.json_response({"sta": "02", "message": "系统异常"})


@pytest.fixture
async def gateway(socket_enabled):
    gateway = Gateway()
    app = web.Application()
    app.router.add_post("/{path:.*}", gateway.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        yield gateway, server, session
    await server.close()


def _client(server: TestServer, session: aiohttp.ClientSession, **kwargs: Any):
    return NanwangClient(session, base_url=str(server.make_url("")), **kwargs)


async def test_app_sms_login_and_rolling_token(gateway) -> None:
    gateway_object, server, session = gateway
    client = _client(server, session)
    updates: list[dict[str, Any]] = []
    client.on_auth_update = lambda: updates.append(client.auth_state)

    await client.async_send_sms("13800000000", "030000")
    assert await client.async_login_sms("13800000000", "123456", "030000") == TOKEN
    assert client.cookies == {"CAMSID": "abc"}

    assert await client.async_get_bound_users() == [USER]
    assert client.token == ROLLED_TOKEN
    assert client.cookies == {"CAMSID": "abc", "SESSION": "session"}
    assert updates[-1]["token"] == ROLLED_TOKEN

    sms, login, users = gateway_object.calls
    assert sms[0] == P_SEND_SMS
    assert login[1] == {
        "phoneNumber": "13800000000",
        "credType": "11",
        "vcCode": "123456",
        "areaCode": "030000",
        "defaultAreaCode": "030000",
    }
    assert login[2]["need-crypto"] == "true"
    assert users[2]["x-auth-token"] == TOKEN
    assert users[2]["Cookie"] == "CAMSID=abc"
    assert users[2]["channelCode"] == "4"


async def test_app_errors_and_guest_token_rejected(gateway) -> None:
    _, server, session = gateway
    client = _client(server, session)
    with pytest.raises(NanwangApiError) as error:
        await client.async_send_sms("0", "030000")
    assert error.value.sta == "01"

    with pytest.raises(NanwangApiError) as error:
        await client.async_login_sms("13800000000", "000000", "030000")
    assert error.value.sta == "0001003"

    client.token = "expired"
    with pytest.raises(NanwangAuthError):
        await client.async_get_bound_users()
    assert client.token == "expired"


@pytest.mark.parametrize("sta", ["0408", "1010", "0101"])
async def test_app_session_expiry_statuses(gateway, sta: str) -> None:
    _, server, session = gateway
    client = _client(server, session, token="expired")
    gateway[0].auth_sta = sta
    with pytest.raises(NanwangAuthError) as error:
        await client.async_get_bound_users()
    assert error.value.sta == sta


async def test_app_queries_and_decryption(gateway) -> None:
    gateway_object, server, session = gateway
    client = _client(server, session, token=TOKEN)

    assert await client.async_get_metering_points(USER) == [POINT]
    assert gateway_object.calls[-1][1] == {
        "areaCode": "030000",
        "eleCustNumberList": [{"eleCustId": "B1", "areaCode": "030000"}],
    }

    bills = await client.async_get_monthly_bills("202611", owner_number=USER["eleCustNumber"])
    assert bills == [
        {
            **BILL,
            "yearMonth": "2026-11",
            "billingElectricity": "250",
            "actualTotalAmount": "150.25",
        }
    ]
    assert gateway_object.calls[-1][0] == P_MONTHLY_BILLS
    assert gateway_object.calls[-1][1]["yearMonth"] == "202611"

    assert await client.async_get_daily(USER, POINT, "202611") == DAILY
    assert gateway_object.calls[-1][1]["meteringPointId"] == "MPID"
    assert gateway_object.calls[-1][1]["meteringPointNumber"] == "MPNO"


async def test_removed_web_capabilities_are_not_exposed(gateway) -> None:
    _, server, session = gateway
    client = _client(server, session)
    assert not hasattr(client, "async_get_account_surplus")
    assert not hasattr(client, "async_get_yearly")
