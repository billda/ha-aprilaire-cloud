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
            try:
                account = await self._async_validate_user_input(user_input)
            except AprilaireCloudAuthenticationError:
                errors["base"] = "invalid_auth"
            except AprilaireCloudCommunicationError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(account[CONF_ACCOUNT_USER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=account[CONF_ACCOUNT_EMAIL],
                    data={CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> config_entries.ConfigFlowResult:
        """Begin a reauth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Confirm and complete reauthentication."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                account = await self._async_validate_user_input(user_input)
            except AprilaireCloudAuthenticationError:
                errors["base"] = "invalid_auth"
            except AprilaireCloudCommunicationError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                if account[CONF_ACCOUNT_USER_ID] != entry.unique_id:
                    errors["base"] = "wrong_account"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        title=account[CONF_ACCOUNT_EMAIL],
                        data={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle credential updates from the UI."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                account = await self._async_validate_user_input(user_input)
            except AprilaireCloudAuthenticationError:
                errors["base"] = "invalid_auth"
            except AprilaireCloudCommunicationError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                if account[CONF_ACCOUNT_USER_ID] != entry.unique_id:
                    errors["base"] = "wrong_account"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        title=account[CONF_ACCOUNT_EMAIL],
                        data={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def _async_validate_user_input(self, user_input: dict[str, Any]) -> dict[str, str]:
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

