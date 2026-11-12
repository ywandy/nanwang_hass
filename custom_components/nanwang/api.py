"""南网在线 Web 端（95598.csg.cn/ucs/ma/wt）异步客户端。

协议移植自 reverse/nanwang/poc/nw.py（2026-11-12 已在真实网关做过无凭据验证）：

* 请求加密：``{"param": Base64(AES-128-CBC-ZeroPadding(JSON))}`` + 请求头 ``need-crypto: true``
* 响应解密：响应头 ``need-decrypto`` 且 ``data`` 为字符串时，用同一 key/iv 解密后 JSON.parse
* 认证：``x-auth-token``（必需）、``custNumber``（有就带）
* 状态码：00 成功 / 01 参数错误 / 02 系统异常 / 04 未登录 / 0500 未绑定户号

本模块只依赖 aiohttp + cryptography（两者都是 Home Assistant core 依赖），不 import homeassistant，
方便单独测试。只调用查询类接口；sendMsg / login 是登录必需的两个有副作用请求，绝不自动重试。
"""

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
API_PREFIX = "/ucs/ma/wt"

# Web bundle 1.6.230 中 47e3 模块混淆串经 cf45.Se() 还原，与社区项目一致
AES_KEY = b"zFujxJSrfmClqtKO"
AES_IV = b"oMShoRXZnAivcCyR"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

STA_SUCCESS = "00"
STA_NO_LOGIN = "04"
STA_NO_DATA = "09"
STA_NO_BINDS = "0500"

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
    "9999": "新账号登录被限制，请稍后再试",
}

P_SEND_MSG = "/center/sendMsg"
P_LOGIN = "/center/login"
P_BIND_USERS = "/eleCustNumber/queryBindEleUsers"
P_METERING = "/charge/queryMeteringPoint"
P_SURPLUS = "/charge/queryUserAccountNumberSurplus"
P_ANALYZE = "/charge/getAnalyzeFeeDetails"
P_DAILY = "/charge/queryDayElectricByMPoint"

# 只保留会话类 cookie，负载均衡 cookie 没意义
_SESSION_COOKIES = ("CAMSID", "SESSION", "JSESSIONID")


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------


def _zero_pad(data: bytes) -> bytes:
    """CryptoJS ZeroPadding：不足 16 字节补 0；已对齐则不追加整块。"""
    return data + b"\x00" * ((-len(data)) % 16)


def encrypt_param(obj: Any, key: bytes = AES_KEY, iv: bytes = AES_IV) -> str:
    """对象 -> 紧凑 JSON -> ZeroPadding -> AES-128-CBC -> Base64（即前端 encryptAES）。"""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(_zero_pad(raw)) + enc.finalize()).decode("ascii")


def decrypt_param(b64: str, key: bytes = AES_KEY, iv: bytes = AES_IV) -> str:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    raw = dec.update(base64.b64decode(b64)) + dec.finalize()
    return raw.rstrip(b"\x00").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class NanwangError(Exception):
    """所有可预期错误的基类。"""


class NanwangConnectionError(NanwangError):
    """网络错误、HTTP 错误、响应不是 JSON、解密失败。"""


class NanwangApiError(NanwangError):
    """业务失败（sta != 00）。"""

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
        """给配置界面看的简短文案。"""
        return STA_HINTS.get(self.sta) or self.server_message or f"sta={self.sta}"


class NanwangAuthError(NanwangApiError):
    """token 失效 / 未登录（sta=04）。"""


class NanwangNoBindsError(NanwangApiError):
    """账号没有绑定户号（sta=0500）。"""


def _check(res: dict[str, Any], path: str) -> Any:
    sta = str(res.get("sta"))
    if sta == STA_SUCCESS:
        return res.get("data")
    if sta == STA_NO_LOGIN:
        raise NanwangAuthError(sta, res.get("message"), path)
    if sta == STA_NO_BINDS:
        raise NanwangNoBindsError(sta, res.get("message"), path)
    raise NanwangApiError(sta, res.get("message"), path)


def ele_cust_id(user: dict[str, Any]) -> str:
    """接口里的 eleCustId 是绑定关系 bindingId，不是 16 位缴费号 eleCustNumber。"""
    return str(user.get("bindingId") or user.get("eleCustId") or "")


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class NanwangClient:
    """JSON POST + 可选 AES + token 自动续期 + need-decrypto 解密。"""

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
        self.custnumber = custnumber
        self.cookies: dict[str, str] = dict(cookies or {})
        # token/custNumber 变化时回调（用于写回 config entry）
        self.on_auth_update: Callable[[], None] | None = None

    @property
    def auth_state(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "custnumber": self.custnumber,
            "cookies": dict(self.cookies),
        }

    def _adopt_auth(self, headers: Any) -> None:
        """只在 sta=00 时采纳响应头里的 token，避免把游客 token 存下来。"""
        changed = False
        new_token = headers.get("x-auth-token")
        if new_token and new_token != self.token:
            self.token = new_token
            changed = True
        cust = headers.get("custnumber")
        if cust and cust != self.custnumber:
            self.custnumber = cust
            changed = True
        if changed and self.on_auth_update is not None:
            self.on_auth_update()

    def _remember_cookies(self, headers: Any) -> None:
        for line in headers.getall("Set-Cookie", []):
            name, sep, value = line.split(";", 1)[0].partition("=")
            if sep and name.strip() in _SESSION_COOKIES:
                self.cookies[name.strip()] = value.strip()

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
        }
        if auth and self.token:
            headers["x-auth-token"] = self.token
        if auth and self.custnumber:
            headers["custNumber"] = self.custnumber
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if crypto:
            headers["need-crypto"] = "true"
            data_obj: dict[str, Any] = {"param": encrypt_param(payload)}
        else:
            data_obj = payload
        data = json.dumps(data_obj, ensure_ascii=False).encode("utf-8")
        url = f"{self._base}{API_PREFIX}{path}"

        for attempt in range(retries + 1):
            try:
                async with self._session.post(
                    url, data=data, headers=headers, timeout=self._timeout
                ) as resp:
                    text = await resp.text(encoding="utf-8", errors="replace")
                    status = resp.status
                    resp_headers = resp.headers
                break
            except (aiohttp.ClientError, TimeoutError) as err:
                if attempt >= retries:
                    raise NanwangConnectionError(f"网络错误 {path}: {err!r}") from err
                await asyncio.sleep(0.5 * (attempt + 1))

        if status >= 400:
            raise NanwangConnectionError(f"HTTP {status} {path}: {text[:200]}")
        self._remember_cookies(resp_headers)
        if not text.strip():
            raise NanwangConnectionError(f"空响应 {path}（HTTP {status}）")

        # 网关偶尔在 JSON 前后夹带杂质，截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise NanwangConnectionError(f"响应不是 JSON {path}: {text[:200]}")
        try:
            res: dict[str, Any] = json.loads(text[start : end + 1])
        except json.JSONDecodeError as err:
            raise NanwangConnectionError(f"响应 JSON 解析失败 {path}") from err

        flag = resp_headers.get("need-decrypto")
        if flag and str(flag).lower() != "false" and isinstance(res.get("data"), str):
            try:
                res["data"] = json.loads(decrypt_param(res["data"]))
            except (ValueError, TypeError) as err:
                raise NanwangConnectionError(f"响应解密失败 {path}: {err}") from err

        _LOGGER.debug("%s -> sta=%s message=%r", path, res.get("sta"), res.get("message"))
        if str(res.get("sta")) == STA_SUCCESS:
            self._adopt_auth(resp_headers)
        return res

    # -- 登录 ----------------------------------------------------------------

    async def async_send_sms(self, phone: str, area_code: str) -> None:
        """发登录短信验证码（vcType=1 登录 / msgType=1 验证码）。"""
        res = await self._post(
            P_SEND_MSG,
            {"areaCode": area_code, "phoneNumber": phone, "vcType": "1", "msgType": "1"},
            auth=False,
            retries=0,
        )
        _check(res, P_SEND_MSG)

    async def async_login_sms(self, phone: str, code: str, area_code: str) -> str:
        """短信验证码登录，成功后 token 在响应头 x-auth-token。"""
        self.token = None
        res = await self._post(
            P_LOGIN,
            {
                "areaCode": area_code,
                "acctId": phone,
                "logonChan": "3",  # 网上营业厅
                "credType": "11",  # 手机短信
                "code": code,
            },
            crypto=True,
            auth=False,
            retries=0,
        )
        sta = str(res.get("sta"))
        if sta != STA_SUCCESS:
            # 登录接口的 sta=04 不是 token 失效，统一按业务失败处理
            raise NanwangApiError(sta, res.get("message"), P_LOGIN)
        if not self.token:
            raise NanwangConnectionError("登录返回 sta=00 但响应头没有 x-auth-token")
        return self.token

    # -- 查询 ----------------------------------------------------------------

    async def async_get_bound_users(self) -> list[dict[str, Any]]:
        """绑定户号列表；没有绑定时返回空列表。"""
        try:
            return _check(await self._post(P_BIND_USERS, {}), P_BIND_USERS) or []
        except NanwangNoBindsError:
            return []

    async def async_get_metering_points(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        area = str(user.get("areaCode") or "")
        body = {
            "areaCode": area,
            "eleCustNumberList": [{"eleCustId": ele_cust_id(user), "areaCode": area}],
        }
        try:
            return _check(await self._post(P_METERING, body, crypto=True), P_METERING) or []
        except NanwangNoBindsError:
            return []

    async def async_get_account_surplus(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        """按计量点的余额：每项含 balance / arrears / lockedBalance（2026-11-12 真实账号实测）。"""
        body = {"areaCode": str(user.get("areaCode") or ""), "eleCustId": ele_cust_id(user)}
        return _check(await self._post(P_SURPLUS, body, crypto=True), P_SURPLUS) or []

    async def async_get_yearly(
        self, user: dict[str, Any], point: dict[str, Any], year: int
    ) -> dict[str, Any]:
        """年度每月电量/电费（getAnalyzeFeeDetails）。

        实测 yearMonth 形如 "2026-01"；meteringPointId 被服务端忽略（真正起作用的是
        eleCustId + areaCode + 年份），保留“先号码后 ID”的回退只为容错。
        """
        area = str(point.get("areaCode") or user.get("areaCode") or "")
        candidates = [
            str(point[k]) for k in ("meteringPointNumber", "meteringPointId") if point.get(k)
        ] or [""]
        last: NanwangApiError | None = None
        for value in candidates:
            body = {
                "areaCode": area,
                "electricityBillYear": year,
                "eleCustId": ele_cust_id(user),
                "meteringPointId": value,
            }
            try:
                return _check(await self._post(P_ANALYZE, body, crypto=True), P_ANALYZE) or {}
            except NanwangAuthError:
                raise
            except NanwangApiError as err:
                last = err
        assert last is not None
        raise last

    async def async_get_daily(
        self, user: dict[str, Any], point: dict[str, Any], year_month: str
    ) -> dict[str, Any]:
        """某月逐日电量（queryDayElectricByMPoint），year_month 为 yyyyMM。

        实测每天只有 date / power(kWh) / maxTemperature / minTemperature，没有电费字段；
        充电桩等无数据户号返回 sta=09。
        """
        base = {
            "areaCode": str(point.get("areaCode") or user.get("areaCode") or ""),
            "eleCustId": ele_cust_id(user),
            "eleCustNumber": str(point.get("eleCustNumber") or user.get("eleCustNumber") or ""),
            "yearMonth": year_month,
            "supplyUnit": str(user.get("supplyUnit") or ""),
        }
        attempts = [
            {**base, "meteringPointId": str(point[k])}
            for k in ("meteringPointId", "meteringPointNumber")
            if point.get(k)
        ]
        attempts.append(base)
        last: NanwangApiError | None = None
        for body in attempts:
            try:
                return _check(await self._post(P_DAILY, body, crypto=True), P_DAILY) or {}
            except NanwangAuthError:
                raise
            except NanwangApiError as err:
                last = err
        assert last is not None
        raise last
