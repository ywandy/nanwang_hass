"""把逐日电量按真实日期写进 HA 长期统计，给能量面板用。

官方日数据滞后 1~2 天；只靠传感器的话，电量会被记在“拉到数据的那一刻”。这里写外部统计
``nanwang:<户号>_energy``：每天一条（当地 0 点），state = 当日 kWh，sum = 累计 kWh。
每次刷新都重写最近 15 天（跨月也一样），迟到或被修正的日数据也能补上。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN, RECENT_DAYS, STATS_INITIAL_MONTHS, STATS_MAX_MONTHS

# unit_class 在较新的 HA 里是必填（2026.11 起缺了会报错）；2025.6 的 recorder 不认识这个键，
# 传了反而建 metadata 失败。按当前版本的 TypedDict 字段决定传不传。
_HAS_UNIT_CLASS = "unit_class" in StatisticMetaData.__annotations__


def energy_statistic_id(number: str) -> str:
    return f"{DOMAIN}:{number}_energy"


def month_start(year_month: str) -> datetime:
    """yyyyMM -> 当地时区该月 1 号 0 点。"""
    return datetime(
        int(year_month[:4]), int(year_month[4:6]), 1, tzinfo=dt_util.get_default_time_zone()
    )


def local_midnight(value: datetime) -> datetime:
    return dt_util.as_local(value).replace(hour=0, minute=0, second=0, microsecond=0)


def recent_start(now: datetime) -> datetime:
    """“最近 RECENT_DAYS 天”的起点：RECENT_DAYS 天前的 0 点（今天有数据也算上）。"""
    return local_midnight(now) - timedelta(days=RECENT_DAYS)


def _months_ago_start(now: datetime, months: int) -> datetime:
    index = now.year * 12 + now.month - 1 - months
    return month_start(f"{index // 12}{index % 12 + 1:02d}")


def months_between(start: datetime, end: datetime) -> list[str]:
    """start 到 end（含）跨过的月份，yyyyMM，从旧到新。"""
    start, end = dt_util.as_local(start), dt_util.as_local(end)
    first, last = start.year * 12 + start.month - 1, end.year * 12 + end.month - 1
    return [f"{i // 12}{i % 12 + 1:02d}" for i in range(first, last + 1)]


def sync_window_start(now: datetime, last_start: datetime | None) -> datetime:
    """本次要（重新）写入统计的起点。

    * 还没有统计：回填最近 STATS_INITIAL_MONTHS 个月；
    * 已有统计：重写最近 RECENT_DAYS 天；如果最后一条统计更早（HA 停过），从那天开始补，
      但最多往回 STATS_MAX_MONTHS 个月。
    """
    if last_start is None:
        return _months_ago_start(now, STATS_INITIAL_MONTHS - 1)
    return max(
        min(recent_start(now), local_midnight(last_start)),
        _months_ago_start(now, STATS_MAX_MONTHS - 1),
    )


async def async_last_statistic(hass: HomeAssistant, statistic_id: str) -> tuple[datetime | None, float]:
    """(最后一条统计的开始时间, 它的 sum)；还没有统计时为 (None, 0)。"""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    if rows := result.get(statistic_id):
        return dt_util.utc_from_timestamp(rows[0]["start"]), rows[0].get("sum") or 0.0
    return None, 0.0


async def async_import_daily_energy(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    window_start: datetime,
    days: list[tuple[datetime, float]],
    fallback_sum: float,
) -> None:
    """从 window_start 起重写逐日统计。

    累计基数取窗口内第一条已有统计的 sum - state（即它之前的累计值）；窗口内还没有统计时，
    说明最后一条统计在窗口之前（断档被截断），沿用 fallback_sum。
    """
    days = sorted((start, kwh) for start, kwh in days if start >= window_start)
    if not days:
        return
    existing = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        window_start,
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    if rows := existing.get(statistic_id):
        total = (rows[0].get("sum") or 0.0) - (rows[0].get("state") or 0.0)
    else:
        total = fallback_sum

    stats: list[StatisticData] = []
    for start, kwh in days:
        total += kwh
        stats.append(StatisticData(start=start, state=kwh, sum=total))

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    if _HAS_UNIT_CLASS:
        metadata["unit_class"] = EnergyConverter.UNIT_CLASS  # type: ignore[typeddict-unknown-key]
    async_add_external_statistics(hass, metadata, stats)
