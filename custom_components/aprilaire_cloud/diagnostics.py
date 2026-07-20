"""Privacy-preserving diagnostics for AprilAire Cloud."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ENABLE_EXTRA_DIAGNOSTICS,
    CONF_FALLBACK_REFRESH_MINUTES,
    CONF_SAFETY_REFRESH_MINUTES,
    DEFAULT_ENABLE_EXTRA_DIAGNOSTICS,
    DEFAULT_FALLBACK_REFRESH_MINUTES,
    DEFAULT_SAFETY_REFRESH_MINUTES,
    DOMAIN,
)
from .data import AprilaireCloudConfigEntry
from .models import DeviceRecord
from .profiles import (
    NormalizedThermostatState,
    capabilities_for_record,
    normalize_device,
    status_requests_for_record,
)


class ExportPseudonymizer:
    """Create referentially consistent labels scoped to one diagnostics export."""

    def __init__(self) -> None:
        """Initialize empty per-kind mappings."""
        self._values: dict[str, dict[Hashable, str]] = {}

    def label(self, kind: str, value: Hashable | None) -> str | None:
        """Return an insertion-ordered pseudonym for a value."""
        if value is None:
            return None
        values = self._values.setdefault(kind, {})
        if value not in values:
            values[value] = f"{kind}_{len(values) + 1}"
        return values[value]


def _payload_shape(value: Any) -> Any:
    """Return keys and value types without returning any payload values."""
    if isinstance(value, dict):
        return {
            str(key): _payload_shape(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "item": _payload_shape(value[0]) if value else None,
        }
    if value is None:
        return "null"
    return type(value).__name__


def _profile_diagnostics(device: DeviceRecord) -> dict[str, Any]:
    """Return value-free profile diagnostics."""
    capabilities = capabilities_for_record(device)
    details: dict[str, Any] = {
        "status_payload_keys": sorted(device.status_payloads),
        "status_request_keys": [
            {"key": request.key, "route": request.endpoint}
            for request in status_requests_for_record(device)
        ],
        "capability_names": sorted(device.capability_names),
        "commands": {
            command.value: {
                "writable": capability.writable,
                "evidence": capability.evidence.value,
                "unavailable_reason": capability.unavailable_reason,
                "unit": capability.unit,
            }
            for command, capability in (capabilities.commands.items() if capabilities else ())
        },
    }

    if device.profile_key == "thermostat":
        normalized = normalize_device(device)
        if isinstance(normalized, NormalizedThermostatState):
            details["thermostat"] = {
                "zones": {
                    zone_key: {
                        "temperature_unit": zone.temperature_unit,
                        "has_current_temperature": zone.current_temperature is not None,
                        "has_current_humidity": zone.current_humidity is not None,
                        "has_heat_setpoint": zone.heat_setpoint is not None,
                        "has_cool_setpoint": zone.cool_setpoint is not None,
                    }
                    for zone_key, zone in sorted(normalized.zones.items())
                },
                "iaq_keys": sorted(normalized.iaq),
            }

    return details


def _device_diagnostics(
    device: DeviceRecord,
    pseudonyms: ExportPseudonymizer,
) -> dict[str, Any]:
    """Return a privacy-safe summary for one integration device."""
    return {
        "location": pseudonyms.label("location", device.hierarchy.location_id),
        "access": device.hierarchy.access,
        "supported": device.supported,
        "unsupported_reason": device.unsupported_reason,
        "profile": device.profile_key,
        "capability_names": sorted(device.capability_names),
        "model": device.device_status.get("model"),
        "firmware": device.device_status.get("firmwareRev"),
        "payload_shapes": {
            "device_status": _payload_shape(device.device_status),
            "device_settings": _payload_shape(device.device_settings),
            "device_setup": _payload_shape(device.device_setup),
            "sensor_hub_status": _payload_shape(device.sensor_hub_status),
            "status_payloads": _payload_shape(device.status_payloads),
        },
        "profile_diagnostics": _profile_diagnostics(device),
    }


async def async_get_config_entry_diagnostics(
    hass,
    entry: AprilaireCloudConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics safe for normal public issue use."""
    runtime_data = entry.runtime_data
    coordinator = runtime_data.coordinator
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    registry_devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    pseudonyms = ExportPseudonymizer()

    locations = {
        pseudonyms.label("location", location_id): {
            "time_zone": location.time_zone,
            "device_count": sum(
                device.hierarchy.location_id == location_id
                for device in coordinator.data.devices.values()
            ),
        }
        for location_id, location in coordinator.data.locations.items()
    }
    devices = {
        pseudonyms.label("device", device_id): _device_diagnostics(device, pseudonyms)
        for device_id, device in coordinator.data.devices.items()
    }
    socket_states = {
        pseudonyms.label("location", location_id): {
            "transport_connected": state.transport_connected,
            "subscription_acknowledged": state.subscription_acknowledged,
            "initial_sync_complete": state.initial_sync_complete,
            "last_received_at": (
                state.last_received_at.isoformat() if state.last_received_at else None
            ),
            "reconnect_attempt": state.reconnect_attempt,
            "last_error": "present" if state.last_error else None,
        }
        for location_id, state in coordinator.data.socket_states.items()
    }
    write_states = {
        pseudonyms.label("device", device_id): {
            "pending_paths": summary["pending_paths"],
            "inflight_paths": summary["inflight_paths"],
            "waiting_for_confirmation": summary["waiting_for_confirmation"],
        }
        for device_id, summary in coordinator.write_state_summaries.items()
    }

    return {
        "privacy": {
            "safe_for_public_issue": True,
            "review_before_sharing": True,
            "pseudonyms_are_export_scoped": True,
            "raw_vendor_payloads_included": False,
        },
        "entry": {
            "state": str(entry.state),
            "configured_fields": sorted(entry.data),
        },
        "snapshot": {
            "authentication": getattr(runtime_data.client, "auth_metadata", None),
            "rate_limited": runtime_data.client.rate_limited_until is not None,
            "location_count": len(locations),
            "device_count": len(devices),
            "locations": locations,
            "devices": devices,
            "socket_states": socket_states,
            "write_states": write_states,
            "cached_unknown_device_messages": {
                pseudonyms.label("device", device_id): count
                for device_id, count in coordinator.unknown_device_message_counts.items()
            },
            "last_rest_refresh_present": coordinator.last_rest_refresh_at is not None,
            "locations_with_websocket_messages": [
                pseudonyms.label("location", location_id)
                for location_id in coordinator.last_websocket_message_at
            ],
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
        "device_registry": [
            {
                "id": pseudonyms.label("registry_device", device.id),
                "integration_device": next(
                    (
                        pseudonyms.label("device", identifier)
                        for domain, identifier in device.identifiers
                        if domain == DOMAIN
                    ),
                    None,
                ),
                "model": device.model,
                "manufacturer": device.manufacturer,
                "firmware": device.sw_version,
                "entities": [
                    {
                        "platform": entity.entity_id.partition(".")[0],
                        "disabled": entity.disabled_by is not None,
                    }
                    for entity in er.async_entries_for_device(entity_registry, device.id)
                ],
            }
            for device in registry_devices
        ],
    }
