"""Pure timestamp-aware state reduction helpers for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .models import (
    DeviceRecord,
    HierarchyDevice,
    HierarchyLocation,
    StateSource,
    StateVersion,
    merge_settings_payload,
)
from .profiles import (
    DeviceCommand,
    evaluate_profile,
    thermostat_iaq_status_key_for_message,
    thermostat_status_key_from_message,
)

_MISSING = object()
_SOURCE_PRECEDENCE = {
    StateSource.REST: 1,
    StateSource.WEBSOCKET: 2,
}
STATUS_MESSAGE_KEYS = {
    "DehumidifierStatus": "dehumidifier",
}
THERMOSTAT_STATUS_MESSAGE_TYPE = "ThermostatStatus"


@dataclass(slots=True)
class DeviceWriteState:
    """Track pending and in-flight settings writes for one device."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_paths: tuple[str, ...] = ()
    inflight_paths: tuple[str, ...] = ()
    inflight_expected: dict[str, Any] = field(default_factory=dict)
    inflight_command: DeviceCommand | None = None
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


def parse_vendor_timestamp(value: Any) -> datetime | None:
    """Parse an AprilAire timestamp as an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _accept_version(
    record: DeviceRecord,
    section: str,
    *,
    source: StateSource,
    as_of: datetime | None,
) -> tuple[bool, dict[str, StateVersion]]:
    """Return whether an update wins and the resulting version mapping."""
    current = record.versions.get(section)
    incoming = StateVersion(as_of=as_of, source=source)
    if current is not None:
        if as_of is None and current.as_of is not None:
            return False, record.versions
        if as_of is not None and current.as_of is not None:
            if as_of < current.as_of:
                return False, record.versions
            if (
                as_of == current.as_of
                and _SOURCE_PRECEDENCE[source] < _SOURCE_PRECEDENCE[current.source]
            ):
                return False, record.versions
        elif (
            as_of is None
            and current.as_of is None
            and _SOURCE_PRECEDENCE[source] < _SOURCE_PRECEDENCE[current.source]
        ):
            return False, record.versions

    versions = dict(record.versions)
    versions[section] = incoming
    return True, versions


def iter_leaf_paths(
    data: dict[str, Any],
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Any]]:
    """Return all leaf paths from a nested payload, preserving falsy values."""
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


def apply_confirmed_device_settings(
    record: DeviceRecord,
    settings: dict[str, Any],
    *,
    source: StateSource = StateSource.WEBSOCKET,
) -> DeviceRecord:
    """Merge an incremental confirmed settings update when it is not stale."""
    accepted, versions = _accept_version(
        record,
        "device_settings",
        source=source,
        as_of=parse_vendor_timestamp(settings.get("asOf")),
    )
    if not accepted:
        return record
    confirmed_settings = merge_settings_payload(record.device_settings, settings)
    return replace(
        record,
        device_settings=confirmed_settings,
        pending_device_settings=remove_matching_settings(
            record.pending_device_settings, confirmed_settings
        ),
        versions=versions,
    )


def apply_full_device_settings(
    record: DeviceRecord,
    settings: dict[str, Any],
    *,
    source: StateSource = StateSource.REST,
) -> DeviceRecord:
    """Replace confirmed settings from a non-stale full REST payload."""
    accepted, versions = _accept_version(
        record,
        "device_settings",
        source=source,
        as_of=parse_vendor_timestamp(settings.get("asOf")),
    )
    if not accepted:
        return record
    confirmed_settings = deepcopy(settings)
    return replace(
        record,
        device_settings=confirmed_settings,
        pending_device_settings=remove_matching_settings(
            record.pending_device_settings, confirmed_settings
        ),
        versions=versions,
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
    supported, reason, profile_key, capability_names = evaluate_profile(record)
    return replace(
        record,
        supported=supported,
        unsupported_reason=reason,
        profile_key=profile_key,
        capability_names=capability_names,
    )


def apply_status_payload(
    record: DeviceRecord,
    key: str,
    payload: dict[str, Any],
    *,
    source: StateSource = StateSource.WEBSOCKET,
    full: bool = False,
) -> DeviceRecord:
    """Apply a timestamp-ordered profile status payload."""
    section = f"status:{key}"
    accepted, versions = _accept_version(
        record,
        section,
        source=source,
        as_of=parse_vendor_timestamp(payload.get("asOf")),
    )
    if not accepted:
        return record
    status_payloads = dict(record.status_payloads)
    current = status_payloads.get(key, {})
    status_payloads[key] = deepcopy(payload) if full else merge_settings_payload(current, payload)
    return replace(record, status_payloads=status_payloads, versions=versions)


def _apply_record_payload(
    record: DeviceRecord,
    *,
    section: str,
    attribute: str,
    payload: dict[str, Any],
    source: StateSource,
    full: bool,
) -> DeviceRecord:
    """Apply one generic timestamped record section."""
    accepted, versions = _accept_version(
        record,
        section,
        source=source,
        as_of=parse_vendor_timestamp(payload.get("asOf")),
    )
    if not accepted:
        return record
    current = getattr(record, attribute)
    value = deepcopy(payload) if full else merge_settings_payload(current, payload)
    if attribute == "device_setup":
        return replace(record, device_setup=value, versions=versions)
    if attribute == "device_status":
        return replace(record, device_status=value, versions=versions)
    if attribute == "sensor_hub_status":
        return replace(record, sensor_hub_status=value, versions=versions)
    raise ValueError("Unsupported state section")


def apply_device_event(record: DeviceRecord, message: dict[str, Any]) -> DeviceRecord:
    """Apply an evidence-backed offline or rescinded device event."""
    if message.get("type") != "offline":
        return record
    occurred = parse_vendor_timestamp(message.get("occurred"))
    rescinded = parse_vendor_timestamp(message.get("rescinded"))
    event_time = rescinded or occurred
    if event_time is None:
        return record
    accepted, versions = _accept_version(
        record,
        "health",
        source=StateSource.WEBSOCKET,
        as_of=event_time,
    )
    if not accepted:
        return record
    return replace(
        record,
        health=replace(
            record.health,
            offline=rescinded is None,
            event_occurred_at=occurred,
            event_rescinded_at=rescinded,
        ),
        versions=versions,
    )


def apply_device_message(record: DeviceRecord, message: dict[str, Any]) -> DeviceRecord:
    """Apply one timestamp-ordered WebSocket payload."""
    message_type = message.get("_type")
    if message_type == THERMOSTAT_STATUS_MESSAGE_TYPE:
        return apply_status_payload(
            record,
            thermostat_status_key_from_message(record, message),
            message,
        )
    if iaq_status_key := thermostat_iaq_status_key_for_message(record, message):
        return apply_status_payload(record, iaq_status_key, message)
    if message_type in STATUS_MESSAGE_KEYS:
        return apply_status_payload(record, STATUS_MESSAGE_KEYS[message_type], message)
    if message_type == "DeviceSettings":
        return apply_confirmed_device_settings(record, message)
    if message_type == "DeviceSetup":
        return _apply_record_payload(
            record,
            section="device_setup",
            attribute="device_setup",
            payload=message,
            source=StateSource.WEBSOCKET,
            full=False,
        )
    if message_type == "DeviceStatus":
        return _apply_record_payload(
            record,
            section="device_status",
            attribute="device_status",
            payload=message,
            source=StateSource.WEBSOCKET,
            full=False,
        )
    if message_type == "SensorHubStatus":
        return _apply_record_payload(
            record,
            section="sensor_hub_status",
            attribute="sensor_hub_status",
            payload=message,
            source=StateSource.WEBSOCKET,
            full=False,
        )
    if message_type == "DeviceEvent":
        return apply_device_event(record, message)
    return record


def apply_rest_device_status(
    record: DeviceRecord,
    payload: dict[str, Any],
) -> DeviceRecord:
    """Apply a full basic REST status response."""
    return _apply_record_payload(
        record,
        section="device_status",
        attribute="device_status",
        payload=payload,
        source=StateSource.REST,
        full=True,
    )


def apply_rest_refresh(
    record: DeviceRecord,
    *,
    device_status: dict[str, Any],
    settings: dict[str, Any],
    status_payloads: dict[str, dict[str, Any]] | None = None,
) -> DeviceRecord:
    """Apply independently fetched REST sections in timestamp order."""
    updated = apply_rest_device_status(record, device_status)
    updated = apply_full_device_settings(updated, settings)
    for key, payload in (status_payloads or {}).items():
        updated = apply_status_payload(
            updated,
            key,
            payload,
            source=StateSource.REST,
            full=True,
        )
    return updated


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
