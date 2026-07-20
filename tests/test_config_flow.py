"""Config flow tests for AprilAire Cloud."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.vendor import (
    AprilaireCloudApiClient,
    AprilaireCloudAuthenticationProtocolError,
    AprilaireCloudAuthenticationTransientError,
    AprilaireCloudInvalidCredentialsError,
    AuthOperation,
)

from .common import (
    PASSWORD,
    USER_ID,
    USERNAME,
    build_device_settings,
    build_hierarchy,
    build_initial_messages,
    build_thermostat_hierarchy,
    build_thermostat_initial_messages,
    build_thermostat_settings,
    build_user,
)


def _mock_supported_device_discovery(monkeypatch, *, messages=None) -> None:
    """Mock the extra device classification calls made by the config flow."""
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_device_settings",
        AsyncMock(return_value=build_device_settings()),
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.config_flow.async_collect_location_messages",
        AsyncMock(return_value=messages or build_initial_messages()),
    )


async def test_user_flow_success(hass, enable_custom_integrations, monkeypatch) -> None:
    """A valid account should create a config entry."""
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient, "async_get_user", AsyncMock(return_value=build_user())
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    _mock_supported_device_discovery(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "create_entry"
    assert result["title"] == USERNAME
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_user_flow_duplicate_account_aborts(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """The same AprilAire account cannot be configured twice."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER_ID, data={})
    entry.add_to_hass(hass)

    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient, "async_get_user", AsyncMock(return_value=build_user())
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    _mock_supported_device_discovery(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reauth_updates_credentials(hass, enable_custom_integrations, monkeypatch) -> None:
    """Reauth should update the stored credentials for the same account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old-password"},
        title=USERNAME,
    )
    entry.add_to_hass(hass)

    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient, "async_get_user", AsyncMock(return_value=build_user())
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    _mock_supported_device_discovery(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == PASSWORD


async def test_invalid_auth_is_reported(hass, enable_custom_integrations, monkeypatch) -> None:
    """Authentication failures should stay on the form."""
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_authenticate",
        AsyncMock(
            side_effect=AprilaireCloudInvalidCredentialsError(
                "NotAuthorizedException", AuthOperation.FULL_LOGIN
            )
        ),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_transient_auth_failure_is_cannot_connect(
    hass, enable_custom_integrations, monkeypatch
) -> None:
    """A Cognito outage must not be presented as invalid credentials."""
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_authenticate",
        AsyncMock(
            side_effect=AprilaireCloudAuthenticationTransientError(
                "ServiceUnavailableException", AuthOperation.FULL_LOGIN
            )
        ),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_auth_protocol_failure_is_unknown(
    hass, enable_custom_integrations, monkeypatch
) -> None:
    """Malformed Cognito output must not start a reauth flow."""
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_authenticate",
        AsyncMock(
            side_effect=AprilaireCloudAuthenticationProtocolError(
                "missing_token_field", AuthOperation.FULL_LOGIN
            )
        ),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown"}


async def test_unexpected_validation_failure_is_unknown(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """An unclassified local failure remains a generic flow error."""
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_authenticate",
        AsyncMock(side_effect=RuntimeError("private detail")),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["errors"] == {"base": "unknown"}


async def test_no_supported_devices_is_reported(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Accounts without supported devices should stay on the setup form."""
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient, "async_get_user", AsyncMock(return_value=build_user())
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    messages = build_initial_messages()
    messages[2] = {
        "_type": "DeviceSetup",
        "deviceId": messages[2]["deviceId"],
        "asOf": messages[2]["asOf"],
        "type": "ventilator",
    }
    _mock_supported_device_discovery(monkeypatch, messages=messages)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_supported_devices"}


async def test_empty_hierarchy_is_reported_without_optional_discovery(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """An account with no hierarchy devices has a complete empty classification."""
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_user",
        AsyncMock(return_value=build_user()),
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value={"locations": []}),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["errors"] == {"base": "no_supported_devices"}


async def test_setup_does_not_false_negative_when_device_setup_arrives_late(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """A partial websocket bootstrap must not block a valid supported account."""
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient, "async_get_user", AsyncMock(return_value=build_user())
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    _mock_supported_device_discovery(
        monkeypatch,
        messages=build_initial_messages()[:2],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "create_entry"
    assert result["title"] == USERNAME


async def test_user_flow_accepts_thermostat_only_account(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """A thermostat-only account should be accepted by config flow."""
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient, "async_get_user", AsyncMock(return_value=build_user())
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_thermostat_hierarchy()),
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_device_settings",
        AsyncMock(return_value=build_thermostat_settings()),
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.config_flow.async_collect_location_messages",
        AsyncMock(return_value=build_thermostat_initial_messages()),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "create_entry"


async def test_incomplete_discovery_does_not_false_negative_setup(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Settings and WebSocket failures leave classification pending, not rejected."""
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_user",
        AsyncMock(return_value=build_user()),
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_device_settings",
        AsyncMock(side_effect=RuntimeError("settings unavailable")),
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.config_flow.async_collect_location_messages",
        AsyncMock(side_effect=RuntimeError("socket unavailable")),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] == "create_entry"


async def test_reconfigure_form_and_wrong_account_error(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Reconfigure renders first and refuses credentials for another account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        title=USERNAME,
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(AprilaireCloudApiClient, "async_authenticate", AsyncMock())
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_user",
        AsyncMock(return_value={"userId": "different-user", "email": USERNAME}),
    )
    monkeypatch.setattr(
        AprilaireCloudApiClient,
        "async_get_hierarchy",
        AsyncMock(return_value=build_hierarchy()),
    )
    _mock_supported_device_discovery(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: "replacement-password"},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "wrong_account"}


async def test_options_flow_updates_refresh_settings(
    hass,
    enable_custom_integrations,
) -> None:
    """Options flow should store refresh and diagnostics settings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        title=USERNAME,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "safety_refresh_minutes": 30,
            "fallback_refresh_minutes": 3,
            "enable_extra_diagnostics": True,
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        "safety_refresh_minutes": 30,
        "fallback_refresh_minutes": 3,
        "enable_extra_diagnostics": True,
    }
