"""数据协调器：一次刷新拉取所有绑定户号的电量/电费，并把逐日电量写进长期统计。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import STA_NO_DATA, NanwangApiError, NanwangAuthError, NanwangClient, NanwangError
from .const import DOMAIN
from .statistics import (
    async_import_daily_energy,
    async_last_statistic,
    energy_statistic_id,
    months_between,
    recent_start,
    sync_window_start,
)

_LOGGER = logging.getLogger(__name__)

type NanwangConfigEntry = ConfigEntry[NanwangCoordinator]
type DailyGetter = Callable[[str], Awaitable[dict[str, Any] | None]]


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latest_bill(yearly: dict[str, Any] | None) -> dict[str, Any] | None:
    """electricAndChargeList 中最近一个有电量的月份（yearMonth 实测形如 2026-10）。"""
    months = (yearly or {}).get("electricAndChargeList") or []
    valid = [m for m in months if to_float(m.get("billingElectricity")) is not None]
    return max(valid, key=lambda m: str(m.get("yearMonth") or ""), default=None)


def latest_day(daily: dict[str, Any] | None) -> dict[str, Any] | None:
    """result 中最近一个有电量的日期。"""
    days = (daily or {}).get("result") or []
    valid = [d for d in days if to_float(d.get("power")) is not None]
    return max(valid, key=lambda d: str(d.get("date") or ""), default=None)


def day_start(day: dict[str, Any]) -> datetime | None:
    """逐日记录的日期 -> 当地 0 点，兼容 2026-10-01 / 20261001 / 2026.10.01。"""
    digits = "".join(ch for ch in str(day.get("date") or "") if ch.isdigit())[:8]
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").replace(tzinfo=dt_util.get_default_time_zone())
    except ValueError:
        return None


def parse_days(daily: dict[str, Any] | None) -> list[tuple[datetime, float]]:
    """result -> [(当地 0 点, kWh)]，跳过还没出电量的日子。"""
    out: list[tuple[datetime, float]] = []
    for d in (daily or {}).get("result") or []:
        kwh, start = to_float(d.get("power")), day_start(d)
        if kwh is not None and start is not None:
            out.append((start, kwh))
    return out


@dataclass(slots=True)
class AccountData:
    """一个绑定户号的数据，字段均为接口原始结构。"""

    user: dict[str, Any]
    point: dict[str, Any] | None = None
    surplus: list[dict[str, Any]] = field(default_factory=list)  # 每计量点余额
    year: int | None = None
    yearly: dict[str, Any] | None = None  # getAnalyzeFeeDetails(当年)
    last_bill: dict[str, Any] | None = None  # 最近有数据的账单月
    month: str | None = None  # yyyyMM
    daily: dict[str, Any] | None = None  # queryDayElectricByMPoint(当月)
    recent_days: list[dict[str, Any]] = field(default_factory=list)  # 最近 RECENT_DAYS 天（可跨月）
    latest_day: dict[str, Any] | None = None

    def surplus_sum(self, key: str) -> float | None:
        values = [v for r in self.surplus if (v := to_float(r.get(key))) is not None]
        return sum(values) if values else None

    @property
    def unit_price(self) -> float | None:
        """最近账单月均价（元/kWh）= 官方电费 ÷ 官方电量。"""
        bill = self.last_bill or {}
        kwh, amount = to_float(bill.get("billingElectricity")), to_float(bill.get("actualTotalAmount"))
        if not kwh or amount is None:
            return None
        return amount / kwh


class NanwangCoordinator(DataUpdateCoordinator[dict[str, AccountData]]):
    """以 eleCustNumber 为 key 汇总所有户号。

    不自己轮询（update_interval=None），由 __init__ 在每天 REFRESH_HOURS 整点触发刷新。
    """

    config_entry: NanwangConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: NanwangConfigEntry, client: NanwangClient
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.client = client
        self._points: dict[str, dict[str, Any] | None] = {}
        client.on_auth_update = self._persist_auth

    @callback
    def _persist_auth(self) -> None:
        """网关下发新 token 时写回 config entry，重启后继续用。"""
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, **self.client.auth_state}
        )

    async def _async_update_data(self) -> dict[str, AccountData]:
        try:
            return await self._fetch_all()
        except NanwangAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NanwangError as err:
            raise UpdateFailed(str(err)) from err

    async def _optional[T](self, aw: Awaitable[T], what: str) -> T | None:
        """单个子查询的业务失败只记日志，不拖垮整次刷新；登录失效和网络错误照常抛出。"""
        try:
            return await aw
        except NanwangAuthError:
            raise
        except NanwangApiError as err:
            # sta=09 是“该计量点没数据”（如充电桩），每轮都会出现，不刷警告
            log = _LOGGER.debug if err.sta == STA_NO_DATA else _LOGGER.warning
            log("%s 查询失败：%s", what, err)
            return None

    async def _fetch_all(self) -> dict[str, AccountData]:
        users = await self.client.async_get_bound_users()
        now = dt_util.now()
        result: dict[str, AccountData] = {}
        for user in users:
            number = str(user.get("eleCustNumber") or "")
            if not number:
                continue
            acc = AccountData(user=user)
            acc.surplus = (
                await self._optional(
                    self.client.async_get_account_surplus(user), f"户号 {number} 余额"
                )
                or []
            )
            acc.point = await self._get_point(user, number)
            if acc.point is not None:
                await self._fill_usage(acc, number, now)
            result[number] = acc
        return result

    async def _get_point(self, user: dict[str, Any], number: str) -> dict[str, Any] | None:
        """计量点基本不变，拿到后缓存；多个计量点时只用第一个。"""
        if self._points.get(number) is None:
            points = await self._optional(
                self.client.async_get_metering_points(user), f"户号 {number} 计量点"
            )
            self._points[number] = points[0] if points else None
        return self._points[number]

    async def _fill_usage(self, acc: AccountData, number: str, now: datetime) -> None:
        client, user, point = self.client, acc.user, acc.point
        assert point is not None

        acc.year = now.year
        acc.yearly = await self._optional(
            client.async_get_yearly(user, point, now.year), f"户号 {number} {now.year} 年度电费"
        )
        acc.last_bill = latest_bill(acc.yearly)
        if acc.last_bill is None:
            # 年初当年还没出账单，往前找一年
            prev_year = await self._optional(
                client.async_get_yearly(user, point, now.year - 1),
                f"户号 {number} {now.year - 1} 年度电费",
            )
            acc.last_bill = latest_bill(prev_year)

        # 同一次刷新里同一个月只查一次（传感器和统计导入共用）
        cache: dict[str, dict[str, Any] | None] = {}

        async def get_daily(year_month: str) -> dict[str, Any] | None:
            if year_month not in cache:
                cache[year_month] = await self._optional(
                    client.async_get_daily(user, point, year_month),
                    f"户号 {number} {year_month} 日电量",
                )
            return cache[year_month]

        acc.month = now.strftime("%Y%m")
        acc.daily = await get_daily(acc.month)

        # 最近 RECENT_DAYS 天，月初会连上个月一起取
        since = recent_start(now)
        recent: list[tuple[datetime, dict[str, Any]]] = []
        for year_month in months_between(since, now):
            for day in (await get_daily(year_month) or {}).get("result") or []:
                start = day_start(day)
                if start is not None and since <= start <= now:
                    recent.append((start, day))
        acc.recent_days = [day for _, day in sorted(recent, key=lambda x: x[0])]
        acc.latest_day = latest_day({"result": acc.recent_days})

        # 最近 15 天都没数据（如充电桩 sta=09）就别再碰统计了
        if acc.latest_day is not None and "recorder" in self.hass.config.components:
            await self._sync_statistics(number, now, get_daily)

    async def _sync_statistics(self, number: str, now: datetime, get_daily: DailyGetter) -> None:
        statistic_id = energy_statistic_id(number)
        last_start, last_sum = await async_last_statistic(self.hass, statistic_id)
        window_start = sync_window_start(now, last_start)
        days: list[tuple[datetime, float]] = []
        for year_month in months_between(window_start, now):
            days.extend(parse_days(await get_daily(year_month)))
        await async_import_daily_energy(
            self.hass, statistic_id, f"南网 {number} 用电量", window_start, days, last_sum
        )
