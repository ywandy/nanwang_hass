"""协议层测试：AES 向量 + 本地 mock 网关（真实 HTTP，不连外网）。"""

from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.nanwang.api import (
    NanwangApiError,
    NanwangAuthError,
    NanwangClient,
    decrypt_param,
    encrypt_param,
)

# 由 reverse/nanwang/poc/nw.py 的纯 Python AES 生成；该实现已与 openssl 对照，且被真实网关成功解密
VECTORS = [
    (
        {"areaCode": "030000", "acctId": "13800000000", "logonChan": "3", "credType": "11", "code": "123456"},
        "9CNb15klDDhfWG2Ff0kCVlKeMp5/iIgz3iWw9ic7fM1hNUkFPvgGBLJ3BQSCKv53lXE9TGjpLL0803Clp2HzrozsC7cOEnXj/UbfMLeAyHOTthL/D3bWnMAzGH94B3Cv",
    ),
    # 恰好 16 字节：ZeroPadding 不追加整块
    ({"a": "01234567"}, "sEkaPP9nsgJXxO4l2tUizg=="),
]

TOKEN = "tok-login"
USER = {"eleCustNumber": "0300001234567890", "bindingId": "B1", "areaCode": "030000"}
POINT = {"meteringPointId": "MPID", "meteringPointNumber": "MPNO", "areaCode": "030000"}


@pytest.mark.parametrize(("obj", "expected"), VECTORS)
def test_encrypt_matches_poc(obj: dict[str, Any], expected: str) -> None:
    assert encrypt_param(obj) == expected
    assert json.loads(decrypt_param(expected)) == obj


class Gateway:
    """最小 mock 网关：行为对齐 95598.csg.cn 的已知响应。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.analyze_reject_number = False

    async def handle(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["path"]
        body = await request.json()
        if request.headers.get("need-crypto") == "true":
            body = json.loads(decrypt_param(body["param"]))
        self.calls.append((path, body, dict(request.headers)))

        if path == "/center/sendMsg":
            if len(body.get("phoneNumber", "")) != 11:
                return web.json_response({"sta": "01", "message": "手机号码格式不正确"})
            return web.json_response({"sta": "00"})
        if path == "/center/login":
            if body.get("code") != "123456":
                return web.json_response({"sta": "0001003", "message": "验证码校验失败!"})
            return web.json_response(
                {"sta": "00", "data": None},
                headers={"x-auth-token": TOKEN, "custnumber": "CUST1",
                         "Set-Cookie": "CAMSID=abc; Path=/"},
            )

        if request.headers.get("x-auth-token") != TOKEN:
            # 网关对未登录请求也会下发游客 token，客户端不能采纳
            return web.json_response({"sta": "04"}, headers={"x-auth-token": "guest"})

        if path == "/eleCustNumber/queryBindEleUsers":
            return web.json_response({"sta": "00", "data": [USER]})
        if path == "/charge/getAnalyzeFeeDetails":
            if self.analyze_reject_number and body["meteringPointId"] == "MPNO":
                return web.json_response({"sta": "01", "message": "参数错误"})
            return web.json_response(
                {"sta": "00", "data": {"totalBillingElectricity": "100", "electricAndChargeList": []}},
                headers={"x-auth-token": "tok-refreshed"},
            )
        if path == "/charge/queryDayElectricByMPoint":
            data = {"totalPower": "12.5", "result": [{"date": "2026-11-01", "power": "3.2"}]}
            return web.json_response(
                {"sta": "00", "data": encrypt_param(data)}, headers={"need-decrypto": "true"}
            )
        if path == "/charge/queryUserAccountNumberSurplus":
            if not body.get("areaCode") or body.get("eleCustId") != "B1":
                return web.json_response({"sta": "01", "message": "入参校验失败"})
            return web.json_response(
                {"sta": "00", "data": [{"balance": "88.80", "arrears": "0", "lockedBalance": "0"}]}
            )
        return web.json_response({"sta": "02", "message": "系统异常"})


@pytest.fixture
async def gateway(socket_enabled):
    gw = Gateway()
    app = web.Application()
    app.router.add_post("/ucs/ma/wt/{path:.*}", gw.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        yield gw, server, session
    await server.close()


def _client(server: TestServer, session: aiohttp.ClientSession, **kwargs: Any) -> NanwangClient:
    return NanwangClient(session, base_url=str(server.make_url("")), **kwargs)


async def test_sms_login_adopts_token_and_cookie(gateway) -> None:
    gw, server, session = gateway
    client = _client(server, session)
    updates: list[dict[str, Any]] = []
    client.on_auth_update = lambda: updates.append(client.auth_state)

    await client.async_send_sms("13800000000", "030000")
    assert await client.async_login_sms("13800000000", "123456", "030000") == TOKEN
    assert client.custnumber == "CUST1"
    assert client.cookies == {"CAMSID": "abc"}
    assert updates[-1]["token"] == TOKEN

    login_body = gw.calls[1][1]
    assert login_body["credType"] == "11" and login_body["logonChan"] == "3"
    assert gw.calls[1][2]["need-crypto"] == "true"

    assert await client.async_get_bound_users() == [USER]
    headers = gw.calls[-1][2]
    assert headers["x-auth-token"] == TOKEN
    assert headers["custNumber"] == "CUST1"
    assert headers["Cookie"] == "CAMSID=abc"


async def test_errors(gateway) -> None:
    _, server, session = gateway
    client = _client(server, session)
    with pytest.raises(NanwangApiError) as err:
        await client.async_send_sms("0", "030000")
    assert err.value.sta == "01"

    with pytest.raises(NanwangApiError) as err:
        await client.async_login_sms("13800000000", "000000", "030000")
    assert err.value.sta == "0001003"
    assert err.value.display == "短信验证码校验失败"

    client.token = "expired"
    with pytest.raises(NanwangAuthError):
        await client.async_get_bound_users()
    assert client.token == "expired"  # 游客 token 没被采纳


async def test_decrypt_response_and_token_refresh(gateway) -> None:
    gw, server, session = gateway
    client = _client(server, session, token=TOKEN)
    daily = await client.async_get_daily(USER, POINT, "202611")
    assert daily["totalPower"] == "12.5"
    assert gw.calls[-1][1]["meteringPointId"] == "MPID"

    yearly = await client.async_get_yearly(USER, POINT, 2026)
    assert yearly["totalBillingElectricity"] == "100"
    assert gw.calls[-1][1]["meteringPointId"] == "MPNO"
    assert client.token == "tok-refreshed"


async def test_yearly_falls_back_to_metering_point_id(gateway) -> None:
    gw, server, session = gateway
    gw.analyze_reject_number = True
    client = _client(server, session, token=TOKEN)
    await client.async_get_yearly(USER, POINT, 2026)
    assert [c[1]["meteringPointId"] for c in gw.calls] == ["MPNO", "MPID"]


async def test_account_surplus(gateway) -> None:
    gw, server, session = gateway
    client = _client(server, session, token=TOKEN)
    rows = await client.async_get_account_surplus(USER)
    assert rows[0]["balance"] == "88.80"
    assert gw.calls[-1][1] == {"areaCode": "030000", "eleCustId": "B1"}
    assert gw.calls[-1][2]["need-crypto"] == "true"
