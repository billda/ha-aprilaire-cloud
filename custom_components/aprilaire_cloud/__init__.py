"""The AprilAire Cloud integration."""

from __future__ import annotations

import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .const import DOMAIN, PLATFORMS
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry, AprilaireCloudRuntimeData
from .vendor import AprilaireCloudApiClient

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config) -> bool:
    """Set up the integration."""
    return True


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry) -> bool:
    """Set up AprilAire Cloud from a config entry."""
    client = AprilaireCloudApiClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=async_get_clientsession(hass),
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass,
        config_entry=entry,
        client=client,
    )
    entry.runtime_data = AprilaireCloudRuntimeData(
        client=client,
        coordinator=coordinator,
        integration=async_get_loaded_integration(hass, entry.domain),
    )
    await coordinator.async_config_entry_first_refresh()
    if entry.unique_id and coordinator.data.user_id and entry.unique_id != coordinator.data.user_id:
        await coordinator.async_shutdown()
        raise ConfigEntryAuthFailed("Configured account does not match the saved config entry")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry: AprilaireCloudConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.coordinator.async_shutdown()
    return unload_ok


async def async_remove_config_entry_device(
    hass, entry: AprilaireCloudConfigEntry, device_entry
) -> bool:
    """Allow removal of stale devices only."""
    coordinator = entry.runtime_data.coordinator
    current_device_ids = set(coordinator.data.devices)
    current_location_ids = {
        f"location_{location_id}" for location_id in coordinator.data.locations
    }
    live_identifiers = current_device_ids | current_location_ids
    return all(
        identifier[0] != DOMAIN or identifier[1] not in live_identifiers
        for identifier in device_entry.identifiers
    )
