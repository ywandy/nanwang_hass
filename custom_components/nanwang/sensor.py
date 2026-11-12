"""南网在线传感器：每个绑定户号一个设备。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import AREA_CODES, DOMAIN, ELE_TYPES, MANUFACTURER
from .coordinator import AccountData, NanwangConfigEntry, NanwangCoordinator, to_float
from .statistics import month_start

CURRENCY_CNY = "CNY"


def _year_start(acc: AccountData) -> datetime | None:
    if acc.year is None:
        return None
    return datetime(acc.year, 1, 1, tzinfo=dt_util.get_default_time_zone())


def _month_start(acc: AccountData) -> datetime | None:
    return month_start(acc.month) if acc.month else None


def _arrears(acc: AccountData) -> float | None:
    value = acc.surplus_sum("arrears")
    return value if value is not None else to_float(acc.user.get("totalOwnedFee"))


def _month_cost_estimated(acc: AccountData) -> float | None:
    kwh, price = to_float((acc.daily or {}).get("totalPower")), acc.unit_price
    return None if kwh is None or price is None else kwh * price




def _bill_attrs(acc: AccountData) -> dict[str, Any]:
    return {"year_month": (acc.last_bill or {}).get("yearMonth")}


@dataclass(frozen=True, kw_only=True)
class NanwangSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[AccountData], float | None]
    attrs_fn: Callable[[AccountData], dict[str, Any]] | None = None
    last_reset_fn: Callable[[AccountData], datetime | None] | None = None


def _energy(**kwargs: Any) -> NanwangSensorEntityDescription:
    return NanwangSensorEntityDescription(
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        **kwargs,
    )


def _money(**kwargs: Any) -> NanwangSensorEntityDescription:
    return NanwangSensorEntityDescription(
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_CNY,
        suggested_display_precision=2,
        **kwargs,
    )


SENSORS: tuple[NanwangSensorEntityDescription, ...] = (
    # queryUserAccountNumberSurplus（按计量点求和）
    _money(key="balance", translation_key="balance", value_fn=lambda a: a.surplus_sum("balance")),
    _money(key="arrears", translation_key="arrears", value_fn=_arrears),
    _money(
        key="locked_balance",
        translation_key="locked_balance",
        value_fn=lambda a: a.surplus_sum("lockedBalance"),
    ),
    # getAnalyzeFeeDetails
    _energy(
        key="year_energy",
        translation_key="year_energy",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda a: to_float((a.yearly or {}).get("totalBillingElectricity")),
        last_reset_fn=_year_start,
    ),
    _money(
        key="year_cost",
        translation_key="year_cost",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda a: to_float((a.yearly or {}).get("totalActualAmount")),
        last_reset_fn=_year_start,
    ),
    _energy(
        key="last_bill_energy",
        translation_key="last_bill_energy",
        value_fn=lambda a: to_float((a.last_bill or {}).get("billingElectricity")),
        attrs_fn=_bill_attrs,
    ),
    _money(
        key="last_bill_cost",
        translation_key="last_bill_cost",
        value_fn=lambda a: to_float((a.last_bill or {}).get("actualTotalAmount")),
        attrs_fn=_bill_attrs,
    ),
    NanwangSensorEntityDescription(
        key="unit_price",
        translation_key="unit_price",
        native_unit_of_measurement=f"{CURRENCY_CNY}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        value_fn=lambda a: a.unit_price,
        attrs_fn=_bill_attrs,
    ),
    # queryDayElectricByMPoint
    _energy(
        key="month_energy",
        translation_key="month_energy",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda a: to_float((a.daily or {}).get("totalPower")),
        # 和 year_month（2026-10）统一成 yyyy-MM
        attrs_fn=lambda a: {"month": f"{a.month[:4]}-{a.month[4:]}" if a.month else None},
        last_reset_fn=_month_start,
    ),
    # 官方没有当月/逐日电费：本月电量 × 最近账单月均价，名字里明确是预估
    _money(
        key="month_cost_estimated",
        translation_key="month_cost_estimated",
        value_fn=_month_cost_estimated,
        attrs_fn=lambda a: {
            "unit_price": None if a.unit_price is None else round(a.unit_price, 4),
            "price_from": (a.last_bill or {}).get("yearMonth"),
        },
    ),
    _energy(
        key="latest_day_energy",
        translation_key="latest_day_energy",
        value_fn=lambda a: to_float((a.latest_day or {}).get("power")),
        attrs_fn=lambda a: {"date": (a.latest_day or {}).get("date")},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanwangConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        NanwangSensor(coordinator, number, description)
        for number in coordinator.data
        for description in SENSORS
    )


class NanwangSensor(CoordinatorEntity[NanwangCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: NanwangSensorEntityDescription

    def __init__(
        self,
        coordinator: NanwangCoordinator,
        number: str,
        description: NanwangSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._number = number
        self._attr_unique_id = f"{number}_{description.key}"
        user = coordinator.data[number].user
        area, ele_type = str(user.get("areaCode") or ""), str(user.get("eleType") or "")
        model = " · ".join(
            x for x in (AREA_CODES.get(area, area), ELE_TYPES.get(ele_type, ele_type)) if x
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, number)},
            name=f"南网 {number}",
            manufacturer=MANUFACTURER,
            model=model or None,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _account(self) -> AccountData | None:
        return (self.coordinator.data or {}).get(self._number)

    @property
    def available(self) -> bool:
        return super().available and self._account is not None

    @property
    def native_value(self) -> float | None:
        acc = self._account
        return None if acc is None else self.entity_description.value_fn(acc)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        acc = self._account
        if acc is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(acc)

    @property
    def last_reset(self) -> datetime | None:
        acc = self._account
        if acc is None or self.entity_description.last_reset_fn is None:
            return None
        return self.entity_description.last_reset_fn(acc)
