"""南网在线（95598.csg.cn）Home Assistant 集成。"""

from __future__ import annotations

import aiohttp
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import NanwangClient
from .const import CONF_COOKIES, CONF_CUSTNUMBER, CONF_TOKEN, DOMAIN
from .coordinator import NanwangConfigEntry, NanwangCoordinator

PLATFORMS = [Platform.SENSOR]

_DATA_SESSION = f"{DOMAIN}_session"


def async_get_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """集成共用一个不存 cookie 的会话。

    cookie 由 NanwangClient 按账号手动管理，避免共享 cookie jar 让多个账号串会话。
    """
    session = hass.data.get(_DATA_SESSION)
    if session is None or session.closed:
        session = hass.data[_DATA_SESSION] = async_create_clientsession(
            hass, auto_cleanup=False, cookie_jar=aiohttp.DummyCookieJar()
        )
        # 全局共享会话不能跟随创建它的 config entry 卸载，否则其他账号和配置流程会失效。
        @callback
        def _async_close_session(_event: Event) -> None:
            session.detach()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _async_close_session)
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

    options = dict(entry.options)

    async def _async_update_options(hass: HomeAssistant, updated_entry: NanwangConfigEntry) -> None:
        # token 续期也会更新 entry，只在用户修改选项时重新加载。
        if updated_entry.options != options:
            await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanwangConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
