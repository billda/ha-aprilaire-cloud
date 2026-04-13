"""Diagnostics support for AprilAire Cloud."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.redact import async_redact_data

from .const import (
    CONF_ENABLE_EXTRA_DIAGNOSTICS,
    CONF_FALLBACK_REFRESH_MINUTES,
    CONF_SAFETY_REFRESH_MINUTES,
    DEFAULT_ENABLE_EXTRA_DIAGNOSTICS,
    DEFAULT_FALLBACK_REFRESH_MINUTES,
    DEFAULT_SAFETY_REFRESH_MINUTES,
)
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


def _hash_value(value: str | None) -> str | None:
    """Return a stable hash for user-identifying strings."""
    if value is None:
        return None
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"sha256:{digest}"


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
            "title": _hash_value(entry.title),
            "unique_id": _hash_value(entry.unique_id),
            "state": str(entry.state),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "snapshot": async_redact_data(
            {
                "user_id": _hash_value(coordinator.data.user_id),
                "email": _hash_value(coordinator.data.email),
                "rate_limited_until": (
                    runtime_data.client.rate_limited_until.isoformat()
                    if runtime_data.client.rate_limited_until is not None
                    else None
                ),
                "locations": {
                    location_id: {
                        "name": _hash_value(location.name),
                        "time_zone": location.time_zone,
                    }
                    for location_id, location in coordinator.data.locations.items()
                },
                "devices": {
                    device_id: {
                        "supported": device.supported,
                        "unsupported_reason": device.unsupported_reason,
                        "profile_key": device.profile_key,
                        "supported_writes": list(device.supported_writes),
                        "location": _hash_value(device.hierarchy.location_name),
                        "room": _hash_value(device.hierarchy.room_name),
                        "model": device.device_status.get("model"),
                        "firmware": device.device_status.get("firmwareRev"),
                        "device_setup": device.device_setup,
                        "device_settings": device.device_settings,
                        "pending_device_settings": device.pending_device_settings,
                        "effective_device_settings": device.effective_device_settings,
                        "status_payloads": device.status_payloads,
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
                "write_states": coordinator.write_state_summaries,
                "cached_unknown_device_messages": coordinator.unknown_device_message_counts,
                "last_rest_refresh": (
                    coordinator.last_rest_refresh_at.isoformat()
                    if coordinator.last_rest_refresh_at is not None
                    else None
                ),
                "last_websocket_message": {
                    location_id: ts.isoformat()
                    for location_id, ts in coordinator.last_websocket_message_at.items()
                },
                "current_refresh_mode": coordinator.current_refresh_mode,
                "current_refresh_interval_seconds": coordinator.current_refresh_interval_seconds,
                "config_options": {
                    "safety_refresh_minutes": entry.options.get(
                        CONF_SAFETY_REFRESH_MINUTES, DEFAULT_SAFETY_REFRESH_MINUTES
                    ),
                    "fallback_refresh_minutes": entry.options.get(
                        CONF_FALLBACK_REFRESH_MINUTES, DEFAULT_FALLBACK_REFRESH_MINUTES
                    ),
                    "enable_extra_diagnostics": entry.options.get(
                        CONF_ENABLE_EXTRA_DIAGNOSTICS, DEFAULT_ENABLE_EXTRA_DIAGNOSTICS
                    ),
                },
            },
            TO_REDACT,
        ),
        "device_registry": [
            {
                "id": device.id,
                "name": _hash_value(device.name),
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
