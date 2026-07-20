"""Config flow for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCOUNT_EMAIL,
    CONF_ACCOUNT_USER_ID,
    CONF_ENABLE_EXTRA_DIAGNOSTICS,
    CONF_FALLBACK_REFRESH_MINUTES,
    CONF_SAFETY_REFRESH_MINUTES,
    DEFAULT_ENABLE_EXTRA_DIAGNOSTICS,
    DEFAULT_FALLBACK_REFRESH_MINUTES,
    DEFAULT_SAFETY_REFRESH_MINUTES,
    DOMAIN,
    LOGGER,
    MAX_FALLBACK_REFRESH_MINUTES,
    MAX_SAFETY_REFRESH_MINUTES,
    MIN_FALLBACK_REFRESH_MINUTES,
    MIN_SAFETY_REFRESH_MINUTES,
)
from .profiles import SupportedDeviceSummary, summarize_supported_devices
from .state import (
    apply_confirmed_device_settings,
    apply_device_message,
    apply_hierarchy,
    evaluate_device_support,
)
from .vendor import (
    AprilaireCloudApiClient,
    AprilaireCloudAuthenticationProtocolError,
    AprilaireCloudAuthenticationTransientError,
    AprilaireCloudCommunicationError,
    AprilaireCloudInvalidCredentialsError,
)
from .vendor.websocket import async_collect_location_messages

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AprilaireCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the AprilAire Cloud config flow."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return AprilaireCloudOptionsFlow(config_entry)

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
        hierarchy = await client.async_get_hierarchy()
        summary, classification_complete = await self._async_get_supported_device_summary(
            client, hierarchy
        )
        if summary.total_devices == 0 or (
            classification_complete and summary.supported_devices == 0
        ):
            raise AprilaireCloudNoSupportedDevicesError
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
        except AprilaireCloudInvalidCredentialsError:
            return None, {"base": "invalid_auth"}
        except (
            AprilaireCloudAuthenticationTransientError,
            AprilaireCloudCommunicationError,
        ):
            return None, {"base": "cannot_connect"}
        except AprilaireCloudAuthenticationProtocolError:
            return None, {"base": "unknown"}
        except AprilaireCloudNoSupportedDevicesError:
            return None, {"base": "no_supported_devices"}
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

    async def _async_get_supported_device_summary(
        self,
        client: AprilaireCloudApiClient,
        hierarchy: dict[str, Any],
    ) -> tuple[SupportedDeviceSummary, bool]:
        """Classify supported devices for the account."""
        _, devices, _ = apply_hierarchy(hierarchy, {})
        if not devices:
            return SupportedDeviceSummary(), True

        device_ids = list(devices)
        settings_results = await asyncio.gather(
            *(client.async_get_device_settings(device_id) for device_id in device_ids),
            return_exceptions=True,
        )
        for device_id, settings in zip(device_ids, settings_results, strict=True):
            if isinstance(settings, BaseException):
                continue
            devices[device_id] = evaluate_device_support(
                apply_confirmed_device_settings(devices[device_id], settings)
            )

        location_ids = list({record.hierarchy.location_id for record in devices.values()})
        location_results = await asyncio.gather(
            *(
                async_collect_location_messages(
                    client=client,
                    session=client.session,
                    location_id=location_id,
                )
                for location_id in location_ids
            ),
            return_exceptions=True,
        )
        websocket_complete = True
        for messages in location_results:
            if isinstance(messages, BaseException):
                websocket_complete = False
                continue
            for message in messages:
                msg_device_id: str | None = message.get("deviceId")
                if msg_device_id is None or msg_device_id not in devices:
                    continue
                devices[msg_device_id] = evaluate_device_support(
                    apply_device_message(devices[msg_device_id], message)
                )

        summary = summarize_supported_devices(list(devices.values()))
        LOGGER.debug(
            "Device classification: %d supported, %d unsupported, %d pending",
            summary.supported_devices,
            summary.unsupported_devices,
            summary.pending_classification_devices,
        )
        classification_complete = (
            websocket_complete and summary.pending_classification_devices == 0
        )
        return summary, classification_complete


class AprilaireCloudNoSupportedDevicesError(Exception):
    """Raised when an account has no supported AprilAire devices."""


class AprilaireCloudOptionsFlow(config_entries.OptionsFlowWithReload):
    """Options flow for AprilAire Cloud."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SAFETY_REFRESH_MINUTES,
                        default=options.get(
                            CONF_SAFETY_REFRESH_MINUTES,
                            DEFAULT_SAFETY_REFRESH_MINUTES,
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SAFETY_REFRESH_MINUTES,
                            max=MAX_SAFETY_REFRESH_MINUTES,
                        ),
                    ),
                    vol.Required(
                        CONF_FALLBACK_REFRESH_MINUTES,
                        default=options.get(
                            CONF_FALLBACK_REFRESH_MINUTES,
                            DEFAULT_FALLBACK_REFRESH_MINUTES,
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_FALLBACK_REFRESH_MINUTES,
                            max=MAX_FALLBACK_REFRESH_MINUTES,
                        ),
                    ),
                    vol.Required(
                        CONF_ENABLE_EXTRA_DIAGNOSTICS,
                        default=options.get(
                            CONF_ENABLE_EXTRA_DIAGNOSTICS,
                            DEFAULT_ENABLE_EXTRA_DIAGNOSTICS,
                        ),
                    ): bool,
                }
            ),
        )
