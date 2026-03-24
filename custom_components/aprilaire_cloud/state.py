"""Pure state reduction helpers for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from .models import DeviceRecord, HierarchyDevice, HierarchyLocation, merge_settings_payload
from .profiles import evaluate_profile

_MISSING = object()


@dataclass(slots=True)
class DeviceWriteState:
    """Track pending and in-flight settings writes for one device."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_paths: tuple[str, ...] = ()
    inflight_paths: tuple[str, ...] = ()
    inflight_expected: dict[str, Any] = field(default_factory=dict)
    last_confirmed_settings: dict[str, Any] = field(default_factory=dict)
    inflight_event: asyncio.Event | None = None

    @property
    def summary(self) -> dict[str, Any]:
        """Return a diagnostics-friendly summary."""
        return {
            "pending_paths": list(self.pending_paths),
            "inflight_paths": list(self.inflight_paths),
            "inflight_expected": deepcopy(self.inflight_expected),
            "last_confirmed_settings": deepcopy(self.last_confirmed_settings),
            "waiting_for_confirmation": self.inflight_event is not None,
        }


def iter_leaf_paths(
    data: dict[str, Any],
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Any]]:
    """Return all leaf paths from a nested payload."""
    leaves: list[tuple[tuple[str, ...], Any]] = []
    for key, value in data.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            leaves.extend(iter_leaf_paths(value, path))
        else:
            leaves.append((path, value))
    return leaves


def format_leaf_paths(data: dict[str, Any]) -> tuple[str, ...]:
    """Return sorted leaf paths as dotted strings."""
    return tuple(sorted(".".join(path) for path, _ in iter_leaf_paths(data)))


def get_nested_value(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Return a nested value from a payload."""
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def settings_match_payload(settings: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Return whether all leaf values from a payload match a settings payload."""
    return all(
        get_nested_value(settings, path) == value for path, value in iter_leaf_paths(payload)
    )


def remove_matching_settings(base: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
    """Drop leaves from `base` that match the same leaf value in `comparison`."""
    cleaned: dict[str, Any] = {}
    for key, value in base.items():
        comparison_value = (
            comparison.get(key, _MISSING) if isinstance(comparison, dict) else _MISSING
        )
        if isinstance(value, dict):
            nested = (
                remove_matching_settings(value, comparison_value)
                if isinstance(comparison_value, dict)
                else deepcopy(value)
            )
            if nested:
                cleaned[key] = nested
            continue
        if comparison_value != value:
            cleaned[key] = deepcopy(value)
    return cleaned


def apply_pending_device_settings(record: DeviceRecord, payload: dict[str, Any]) -> DeviceRecord:
    """Merge optimistic local settings into the pending override layer."""
    return replace(
        record,
        pending_device_settings=merge_settings_payload(record.pending_device_settings, payload),
    )


def apply_confirmed_device_settings(record: DeviceRecord, settings: dict[str, Any]) -> DeviceRecord:
    """Update confirmed remote settings and clear matching optimistic overrides."""
    confirmed_settings = merge_settings_payload(record.device_settings, settings)
    return replace(
        record,
        device_settings=confirmed_settings,
        pending_device_settings=remove_matching_settings(
            record.pending_device_settings, confirmed_settings
        ),
    )


def clear_pending_device_settings(record: DeviceRecord, payload: dict[str, Any]) -> DeviceRecord:
    """Remove matching optimistic override paths from the pending layer."""
    return replace(
        record,
        pending_device_settings=remove_matching_settings(record.pending_device_settings, payload),
    )


def pending_payload_is_current(record: DeviceRecord, payload: dict[str, Any]) -> bool:
    """Return whether a request still matches the latest pending local override."""
    return settings_match_payload(record.pending_device_settings, payload)


def evaluate_device_support(record: DeviceRecord) -> DeviceRecord:
    """Determine whether a device should be exposed to Home Assistant."""
    supported, reason, profile_key, supported_writes = evaluate_profile(record)
    return replace(
        record,
        supported=supported,
        unsupported_reason=reason,
        profile_key=profile_key,
        supported_writes=supported_writes,
    )


def apply_device_message(record: DeviceRecord, message: dict[str, Any]) -> DeviceRecord:
    """Apply one websocket payload to a device record."""
    message_type = message.get("_type")
    if message_type == "DehumidifierStatus":
        return replace(record, dehumidifier_status=message)
    if message_type == "DeviceSettings":
        return apply_confirmed_device_settings(record, message)
    if message_type == "DeviceSetup":
        return replace(record, device_setup=message)
    if message_type == "DeviceStatus":
        return replace(record, device_status=message)
    if message_type == "SensorHubStatus":
        return replace(record, sensor_hub_status=message)
    return record


def apply_rest_refresh(
    record: DeviceRecord,
    *,
    device_status: dict[str, Any],
    dehumidifier_status: dict[str, Any],
    settings: dict[str, Any],
) -> DeviceRecord:
    """Apply a REST refresh to a device record."""
    return replace(
        apply_confirmed_device_settings(record, settings),
        device_status=device_status,
        dehumidifier_status=dehumidifier_status,
    )


def apply_hierarchy(
    hierarchy: dict[str, Any],
    existing_devices: dict[str, DeviceRecord],
) -> tuple[dict[str, HierarchyLocation], dict[str, DeviceRecord], set[str]]:
    """Merge hierarchy data and return locations, devices, and removed IDs."""
    previous_device_ids = set(existing_devices)
    locations: dict[str, HierarchyLocation] = {}
    devices: dict[str, DeviceRecord] = {}

    for location in hierarchy.get("locations", []):
        location_id = location["locationId"]
        locations[location_id] = HierarchyLocation(
            location_id=location_id,
            name=location.get("name", location_id),
            time_zone=location.get("timeZone"),
        )
        for room in location.get("rooms", []):
            for device in room.get("devices", []):
                device_id = device["deviceId"]
                hierarchy_device = HierarchyDevice(
                    device_id=device_id,
                    location_id=location_id,
                    location_name=location.get("name", location_id),
                    room_name=room.get("name"),
                    access=device.get("access"),
                    zone=device.get("zone"),
                )
                existing = existing_devices.get(device_id)
                if existing is None:
                    devices[device_id] = DeviceRecord(hierarchy=hierarchy_device)
                else:
                    devices[device_id] = replace(existing, hierarchy=hierarchy_device)

    devices = {device_id: evaluate_device_support(record) for device_id, record in devices.items()}
    return locations, devices, previous_device_ids - set(devices)
