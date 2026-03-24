"""Diagnostics support for AprilAire Cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.redact import async_redact_data

from .data import AprilaireCloudConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "token",
    "id_token",
    "access_token",
    "refresh_token",
    "password",
    "username",
}


async def async_get_config_entry_diagnostics(
    hass,
    entry: AprilaireCloudConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = entry.runtime_data
    coordinator = runtime_data.coordinator
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "unique_id": entry.unique_id,
            "state": str(entry.state),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "snapshot": async_redact_data(
            {
                "user_id": coordinator.data.user_id,
                "email": coordinator.data.email,
                "locations": {
                    location_id: {
                        "name": location.name,
                        "time_zone": location.time_zone,
                    }
                    for location_id, location in coordinator.data.locations.items()
                },
                "devices": {
                    device_id: {
                        "supported": device.supported,
                        "unsupported_reason": device.unsupported_reason,
                        "location": device.hierarchy.location_name,
                        "room": device.hierarchy.room_name,
                        "model": device.device_status.get("model"),
                        "firmware": device.device_status.get("firmwareRev"),
                        "device_setup": device.device_setup,
                        "device_settings": device.device_settings,
                        "dehumidifier_status": device.dehumidifier_status,
                    }
                    for device_id, device in coordinator.data.devices.items()
                },
                "socket_states": {
                    location_id: {
                        "connected": state.connected,
                        "initial_sync_complete": state.initial_sync_complete,
                        "reconnect_attempt": state.reconnect_attempt,
                        "last_error": state.last_error,
                    }
                    for location_id, state in coordinator.data.socket_states.items()
                },
            },
            TO_REDACT,
        ),
        "device_registry": [
            {
                "id": device.id,
                "name": device.name,
                "model": device.model,
                "manufacturer": device.manufacturer,
                "sw_version": device.sw_version,
                "entities": [
                    {
                        "entity_id": entity.entity_id,
                        "unique_id": entity.unique_id,
                        "disabled_by": entity.disabled_by.value if entity.disabled_by else None,
                    }
                    for entity in er.async_entries_for_device(entity_registry, device.id)
                ],
            }
            for device in devices
        ],
    }

