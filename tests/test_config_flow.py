"""Config flow tests for AprilAire Cloud."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.api import (
    AprilaireCloudApiClient,
    AprilaireCloudAuthenticationError,
)
from custom_components.aprilaire_cloud.const import DOMAIN

from .common import PASSWORD, USER_ID, USERNAME, build_hierarchy, build_user


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
        AsyncMock(side_effect=AprilaireCloudAuthenticationError("bad auth")),
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
