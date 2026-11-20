"""Async client for the CSG Nanwang App/mPaaS electricity endpoints."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://95598.csg.cn"
P_SEND_SMS = "/mp/w2/wx/component/common/sendMsgValidation"
P_LOGIN = "/mp/w2/wx/userauth/user/manage/login"
P_USERS = "/mp/w2/wx/portal/eleCustNumber/queryBindEleUsers"
P_METERING = "/mp/w2/wx/elecmanageImp/electric-fee/queryMeteringPointList"
P_MONTHLY_BILLS = "/mp/w2/wx/elecmanageImp/electricity-bill/queryElecMultiBillList"
P_DAILY = "/mp/w2/wx/power-eff/electric-daily/queryCalendarChartDC"

# Restored from App mini-programs 0000000000000111 and 0000000000000123.
AES_KEY = b"cOdHFNHUNkZrjNaN"
AES_IV = b"oMChoRLZnTivcQyR"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; V2548A Build/BP2A.250605.031.A3; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 "
    "Mobile Safari/537.36"
)

STA_SUCCESS = "00"
STA_NO_DATA = "09"
STA_NO_BINDS = "0500"
STA_AUTH_GROUP = {"04", "0408", "1010", "0101"}

STA_HINTS = {
    "09": "该计量点没有数据（例如长期未用电的充电桩）",
    "0009": "获取验证码过于频繁，请明天再试",
    "0005": "验证码输错 5 次，请 30 分钟后重新获取",
    "0006": "验证码已失效，请重新获取",
    "0001003": "短信验证码校验失败",
    "0001002": "凭证错误（手机号未注册或验证码不对）",
    "00010001": "该手机号未注册/未开通在线服务",
    "0408": "账号已在别处登录（被踢下线）",
    "1010": "手机号被其他账户绑定，被强制下线",
    "0101": "登录方式已变化，请重新登录",
    "9999": "新账号登录被限制，请稍后重试",
}

_SESSION_COOKIES = ("CAMSID", "SESSION", "JSESSIONID")


def _zero_pad(data: bytes) -> bytes:
    return data + b"\x00" * ((-len(data)) % 16)


def encrypt_param(obj: Any, key: bytes = AES_KEY, iv: bytes = AES_IV) -> str:
    """Apply the App bundle's AES-128-CBC ZeroPadding envelope."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(_zero_pad(raw)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_param(encoded: str, key: bytes = AES_KEY, iv: bytes = AES_IV) -> str:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    raw = decryptor.update(base64.b64decode(encoded)) + decryptor.finalize()
    return raw.rstrip(b"\x00").decode("utf-8", "replace")


class NanwangError(Exception):
    """Base class for expected integration errors."""


class NanwangConnectionError(NanwangError):
    """Network, HTTP, JSON, or decryption failure."""


class NanwangApiError(NanwangError):
    """Business failure (sta is not 00)."""

    def __init__(self, sta: str, message: str | None, path: str) -> None:
        self.sta = str(sta)
        self.server_message = message or ""
        self.path = path
        text = f"sta={self.sta} {self.server_message}".strip()
        if hint := STA_HINTS.get(self.sta):
            text += f"（{hint}）"
        super().__init__(f"{text} [{path}]")

    @property
    def display(self) -> str:
        return STA_HINTS.get(self.sta) or self.server_message or f"sta={self.sta}"


class NanwangAuthError(NanwangApiError):
    """The App session is invalid and the user must sign in again."""


class NanwangNoBindsError(NanwangApiError):
    """The account has no bound electricity accounts."""


def _check(res: dict[str, Any], path: str) -> Any:
    sta = str(res.get("sta"))
    if sta == STA_SUCCESS:
        return res.get("data")
    if sta in STA_AUTH_GROUP:
        raise NanwangAuthError(sta, res.get("message"), path)
    if sta == STA_NO_BINDS:
        raise NanwangNoBindsError(sta, res.get("message"), path)
    raise NanwangApiError(sta, res.get("message"), path)


def ele_cust_id(user: dict[str, Any]) -> str:
    return str(user.get("bindingId") or user.get("eleCustId") or "")


class NanwangClient:
    """Aiohttp client preserving the integration's public façade."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        token: str | None = None,
        custnumber: str | None = None,
        cookies: dict[str, str] | None = None,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.token = token
        # Kept for existing config entries; the App channel does not send this header.
        self.custnumber = custnumber
        self.cookies: dict[str, str] = dict(cookies or {})
        self.on_auth_update: Callable[[], None] | None = None

    @property
    def auth_state(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "custnumber": self.custnumber,
            "cookies": dict(self.cookies),
        }

    def _adopt_auth(self, headers: Any) -> None:
        changed = False
        new_token = headers.get("x-auth-token")
        if new_token and new_token != self.token:
            self.token = new_token
            changed = True
        for line in headers.getall("Set-Cookie", []):
            name_value = line.split(";", 1)[0]
            name, separator, value = name_value.strip().partition("=")
            if separator and name in _SESSION_COOKIES and self.cookies.get(name) != value:
                self.cookies[name] = value.strip()
                changed = True
        if changed and self.on_auth_update is not None:
            self.on_auth_update()

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        crypto: bool = False,
        auth: bool = True,
        retries: int = 2,
    ) -> dict[str, Any]:
        payload = dict(body or {})
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "User-Agent": USER_AGENT,
            "channelCode": "4",
            "need-crypto": "false",
        }
        if auth and self.token:
            headers["x-auth-token"] = self.token
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        if crypto:
            headers["need-crypto"] = "true"
            request_body: dict[str, Any] = {"param": encrypt_param(payload)}
        else:
            request_body = payload
        data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        url = f"{self._base}{path}"

        for attempt in range(retries + 1):
            try:
                async with self._session.post(
                    url, data=data, headers=headers, timeout=self._timeout
                ) as response:
                    text = await response.text(encoding="utf-8", errors="replace")
                    status = response.status
                    response_headers = response.headers
                break
            except (aiohttp.ClientError, TimeoutError) as error:
                if attempt >= retries:
                    raise NanwangConnectionError(f"网络错误 {path}: {error!r}") from error
                await asyncio.sleep(0.5 * (attempt + 1))

        if status >= 400:
            raise NanwangConnectionError(f"HTTP {status} {path}: {text[:200]}")
        if not text.strip():
            raise NanwangConnectionError(f"空响应 {path}（HTTP {status}）")

        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise NanwangConnectionError(f"响应不是 JSON {path}: {text[:200]}")
        try:
            result: dict[str, Any] = json.loads(text[start : end + 1])
        except json.JSONDecodeError as error:
            raise NanwangConnectionError(f"响应 JSON 解析失败 {path}") from error

        decrypt_flag = response_headers.get("need-decrypto")
        if decrypt_flag and decrypt_flag.lower() != "false" and isinstance(result.get("data"), str):
            try:
                result["data"] = json.loads(decrypt_param(result["data"]))
            except (ValueError, TypeError) as error:
                raise NanwangConnectionError(f"响应解密失败 {path}: {error}") from error

        _LOGGER.debug("%s -> sta=%s message=%r", path, result.get("sta"), result.get("message"))
        if str(result.get("sta")) == STA_SUCCESS:
            self._adopt_auth(response_headers)
        return result

    async def async_send_sms(self, phone: str, area_code: str) -> None:
        result = await self._post(
            P_SEND_SMS,
            {"phoneNumber": phone, "vcType": "1"},
            auth=False,
            retries=0,
        )
        _check(result, P_SEND_SMS)

    async def async_login_sms(self, phone: str, code: str, area_code: str) -> str:
        self.token = None
        result = await self._post(
            P_LOGIN,
            {
                "phoneNumber": phone,
                "credType": "11",
                "vcCode": code,
                "areaCode": area_code,
                "defaultAreaCode": area_code,
            },
            crypto=True,
            auth=False,
            retries=0,
        )
        _check(result, P_LOGIN)
        if not self.token:
            raise NanwangConnectionError("登录成功但响应头没有 x-auth-token")
        return self.token

    async def async_get_bound_users(self) -> list[dict[str, Any]]:
        try:
            return _check(await self._post(P_USERS, {}), P_USERS) or []
        except NanwangNoBindsError:
            return []

    async def async_get_metering_points(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        area = str(user.get("areaCode") or "")
        body = {
            "areaCode": area,
            "eleCustNumberList": [{"eleCustId": ele_cust_id(user), "areaCode": area}],
        }
        try:
            return _check(await self._post(P_METERING, body), P_METERING) or []
        except NanwangNoBindsError:
            return []

    async def async_get_monthly_bills(
        self, year_month: str, cust_number: str = "", *, owner_number: str | None = None
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            body = {
                "custNumber": cust_number,
                "yearMonth": year_month,
                "pageSize": 50,
                "pageNum": page,
                "keyword": "",
            }
            data = _check(await self._post(P_MONTHLY_BILLS, body), P_MONTHLY_BILLS)
            rows: list[dict[str, Any]] = []
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = (
                    data.get("billUserAndYearMonth")
                    or data.get("billList")
                    or data.get("list")
                    or data.get("records")
                    or data.get("rows")
                    or []
                )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if owner_number and str(row.get("eleCustNumber") or "") != owner_number:
                    continue
                bill = dict(row)
                month = str(row.get("electricityBillYearMonth") or row.get("yearMonth") or "")
                if len(month) == 6:
                    month = f"{month[:4]}-{month[4:]}"
                bill["yearMonth"] = month
                bill["billingElectricity"] = row.get("totalPower")
                bill["actualTotalAmount"] = row.get("receieElectricity")
                output.append(bill)
            total = data.get("total") if isinstance(data, dict) else None
            if not rows or total is None or len(output) >= int(total):
                break
            page += 1
        return output

    async def async_get_daily(
        self, user: dict[str, Any], point: dict[str, Any], year_month: str
    ) -> dict[str, Any]:
        body = {
            "areaCode": str(user.get("areaCode") or ""),
            "eleCustId": ele_cust_id(user),
            "eleCustNumber": str(user.get("eleCustNumber") or user.get("custNumber") or ""),
            "meteringPointId": str(point.get("meteringPointId") or ""),
            "meteringPointNumber": str(point.get("meteringPointNumber") or ""),
            "yearMonth": year_month,
            "supplyUnit": str(user.get("supplyUnit") or ""),
        }
        return _check(await self._post(P_DAILY, body), P_DAILY) or {}
