"""配置流程：短信验证码登录 / 导入浏览器 token，支持重新认证。"""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_get_session
from .api import BASE_URL, NanwangApiError, NanwangAuthError, NanwangClient, NanwangError
from .const import (
    AREA_CODES,
    AUTH_SMS,
    AUTH_TOKEN,
    CONF_AREA_CODE,
    CONF_AUTH_METHOD,
    CONF_CODE,
    CONF_CUSTNUMBER,
    CONF_PHONE,
    CONF_TOKEN,
    DEFAULT_AREA_CODE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PHONE_RE = re.compile(r"1\d{10}")

AREA_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[SelectOptionDict(value=k, label=f"{v}（{k}）") for k, v in AREA_CODES.items()],
        mode=SelectSelectorMode.DROPDOWN,
    )
)

SMS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): TextSelector(TextSelectorConfig(type=TextSelectorType.TEL)),
        vol.Required(CONF_AREA_CODE, default=DEFAULT_AREA_CODE): AREA_SELECTOR,
    }
)
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})
TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_CUSTNUMBER): str,
        vol.Required(CONF_AREA_CODE, default=DEFAULT_AREA_CODE): AREA_SELECTOR,
    }
)


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone


class NanwangConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._client: NanwangClient | None = None
        self._phone = ""
        self._area = DEFAULT_AREA_CODE

    def _new_client(self, **kwargs: Any) -> NanwangClient:
        return NanwangClient(async_get_session(self.hass), **kwargs)

    # -- 入口 ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[AUTH_SMS, AUTH_TOKEN])

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._phone = entry_data.get(CONF_PHONE) or ""
        self._area = entry_data.get(CONF_AREA_CODE) or DEFAULT_AREA_CODE
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reauth_confirm", menu_options=[AUTH_SMS, AUTH_TOKEN])

    # -- 短信登录 --------------------------------------------------------------

    async def async_step_sms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"message": ""}
        if user_input is not None:
            self._phone = user_input[CONF_PHONE].strip()
            self._area = user_input[CONF_AREA_CODE]
            if not PHONE_RE.fullmatch(self._phone):
                errors[CONF_PHONE] = "invalid_phone"
            else:
                self._client = self._new_client()
                try:
                    await self._client.async_send_sms(self._phone, self._area)
                except NanwangApiError as err:
                    errors["base"] = "sms_failed"
                    placeholders["message"] = err.display
                except NanwangError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("发送短信验证码时出现意外错误")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_sms_code()
        return self.async_show_form(
            step_id="sms",
            data_schema=self.add_suggested_values_to_schema(
                SMS_SCHEMA, {CONF_PHONE: self._phone, CONF_AREA_CODE: self._area}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_sms_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"phone": mask_phone(self._phone), "message": ""}
        if user_input is not None:
            assert self._client is not None
            try:
                await self._client.async_login_sms(
                    self._phone, user_input[CONF_CODE].strip(), self._area
                )
                users = await self._client.async_get_bound_users()
            except NanwangApiError as err:
                errors["base"] = "login_failed"
                placeholders["message"] = err.display
            except NanwangError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("短信登录时出现意外错误")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(AUTH_SMS, users)
        return self.async_show_form(
            step_id="sms_code",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- 导入 token ------------------------------------------------------------

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"message": "", "url": BASE_URL}
        if user_input is not None:
            self._area = user_input[CONF_AREA_CODE]
            self._client = self._new_client(
                token=user_input[CONF_TOKEN].strip(),
                custnumber=(user_input.get(CONF_CUSTNUMBER) or "").strip() or None,
            )
            try:
                users = await self._client.async_get_bound_users()
            except NanwangAuthError:
                errors["base"] = "invalid_auth"
            except NanwangApiError as err:
                errors["base"] = "api_error"
                placeholders["message"] = err.display
            except NanwangError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("校验 token 时出现意外错误")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(AUTH_TOKEN, users)
        return self.async_show_form(
            step_id="token",
            data_schema=self.add_suggested_values_to_schema(
                TOKEN_SCHEMA, {CONF_AREA_CODE: self._area}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- 收尾 ------------------------------------------------------------------

    async def _async_finish(
        self, method: str, users: list[dict[str, Any]]
    ) -> ConfigFlowResult:
        client = self._client
        assert client is not None
        if not users:
            return self.async_abort(reason="no_accounts")

        data: dict[str, Any] = {
            CONF_AUTH_METHOD: method,
            CONF_AREA_CODE: self._area,
            **client.auth_state,
        }
        if self._phone:
            data[CONF_PHONE] = self._phone

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )

        numbers = sorted(str(u.get("eleCustNumber") or "") for u in users)
        await self.async_set_unique_id(client.custnumber or self._phone or ",".join(numbers))
        self._abort_if_unique_id_configured(updates=data)
        title = f"南网 {mask_phone(self._phone)}" if self._phone else f"南网 {numbers[0]}"
        return self.async_create_entry(title=title, data=data)

