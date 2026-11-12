"""南网在线（95598.csg.cn）Home Assistant 集成。"""

from __future__ import annotations

from datetime import datetime

import aiohttp
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_change

from .api import NanwangClient
from .const import CONF_COOKIES, CONF_CUSTNUMBER, CONF_TOKEN, DOMAIN, REFRESH_HOURS
from .coordinator import NanwangConfigEntry, NanwangCoordinator

PLATFORMS = [Platform.SENSOR]

_DATA_SESSION = f"{DOMAIN}_session"


def async_get_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """集成共用一个不存 cookie 的会话。

    cookie 由 NanwangClient 按账号手动管理，避免共享 cookie jar 让多个账号串会话。
    """
    session = hass.data.get(_DATA_SESSION)
    if session is None:
        session = hass.data[_DATA_SESSION] = async_create_clientsession(
            hass, cookie_jar=aiohttp.DummyCookieJar()
        )
    return session


async def async_setup_entry(hass: HomeAssistant, entry: NanwangConfigEntry) -> bool:
    client = NanwangClient(
        async_get_session(hass),
        token=entry.data[CONF_TOKEN],
        custnumber=entry.data.get(CONF_CUSTNUMBER),
        cookies=entry.data.get(CONF_COOKIES),
    )
    coordinator = NanwangCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    async def _scheduled_refresh(now: datetime) -> None:
        await coordinator.async_refresh()

    # 启动时拉一次，之后只在每天固定整点刷新
    entry.async_on_unload(
        async_track_time_change(
            hass, _scheduled_refresh, hour=list(REFRESH_HOURS), minute=0, second=0
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanwangConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
