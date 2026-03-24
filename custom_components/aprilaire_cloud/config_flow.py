"""Config flow for AprilAire Cloud."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AprilaireCloudApiClient,
    AprilaireCloudAuthenticationError,
    AprilaireCloudCommunicationError,
)
from .const import CONF_ACCOUNT_EMAIL, CONF_ACCOUNT_USER_ID, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AprilaireCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the AprilAire Cloud config flow."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle initial setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            account, errors = await self._async_try_validate_user_input(user_input)
            if not errors and account is not None:
                await self.async_set_unique_id(account[CONF_ACCOUNT_USER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=account[CONF_ACCOUNT_EMAIL],
                    data=self._entry_data_from_user_input(user_input),
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Begin a reauth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Confirm and complete reauthentication."""
        entry = self._get_reauth_entry()
        result = await self._async_handle_entry_credentials_step(
            entry=entry,
            user_input=user_input,
            success_abort_reason="reauth_successful",
            step_id="reauth_confirm",
        )
        if result is not None:
            return result

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA, errors={}
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle credential updates from the UI."""
        entry = self._get_reconfigure_entry()
        result = await self._async_handle_entry_credentials_step(
            entry=entry,
            user_input=user_input,
            success_abort_reason="reconfigure_successful",
            step_id="reconfigure",
        )
        if result is not None:
            return result

        return self.async_show_form(step_id="reconfigure", data_schema=STEP_USER_SCHEMA, errors={})

    async def _async_validate_user_input(
        self,
        user_input: dict[str, Any],
    ) -> dict[str, str]:
        """Validate credentials and return account metadata."""
        client = AprilaireCloudApiClient(
            username=user_input[CONF_USERNAME],
            password=user_input[CONF_PASSWORD],
            session=async_get_clientsession(self.hass),
        )
        await client.async_authenticate()
        user = await client.async_get_user()
        await client.async_get_hierarchy()
        return {
            CONF_ACCOUNT_USER_ID: str(user["userId"]),
            CONF_ACCOUNT_EMAIL: user.get("email", user_input[CONF_USERNAME]),
        }

    async def _async_try_validate_user_input(
        self,
        user_input: dict[str, Any],
    ) -> tuple[dict[str, str] | None, dict[str, str]]:
        """Validate credentials and translate exceptions into flow errors."""
        try:
            return await self._async_validate_user_input(user_input), {}
        except AprilaireCloudAuthenticationError:
            return None, {"base": "invalid_auth"}
        except AprilaireCloudCommunicationError:
            return None, {"base": "cannot_connect"}
        except Exception:
            return None, {"base": "unknown"}

    def _entry_data_from_user_input(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Build config-entry data from submitted credentials."""
        return {
            CONF_USERNAME: user_input[CONF_USERNAME],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
        }

    async def _async_handle_entry_credentials_step(
        self,
        *,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any] | None,
        success_abort_reason: str,
        step_id: str,
    ) -> config_entries.ConfigFlowResult | None:
        """Handle a credential update flow for an existing entry."""
        if user_input is None:
            return None

        account, errors = await self._async_try_validate_user_input(user_input)
        if errors:
            return self.async_show_form(
                step_id=step_id, data_schema=STEP_USER_SCHEMA, errors=errors
            )

        assert account is not None
        if account[CONF_ACCOUNT_USER_ID] != entry.unique_id:
            return self.async_show_form(
                step_id=step_id,
                data_schema=STEP_USER_SCHEMA,
                errors={"base": "wrong_account"},
            )

        self.hass.config_entries.async_update_entry(
            entry,
            title=account[CONF_ACCOUNT_EMAIL],
            data=self._entry_data_from_user_input(user_input),
        )
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason=success_abort_reason)
