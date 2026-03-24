"""Data models for the AprilAire Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def merge_settings_payload(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a deep merge of two settings payloads."""
    from copy import deepcopy

    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings_payload(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


@dataclass(frozen=True, slots=True)
class HierarchyLocation:
    """A location returned from the hierarchy endpoint."""

    location_id: str
    name: str
    time_zone: str | None = None


@dataclass(frozen=True, slots=True)
class HierarchyDevice:
    """A single hierarchy device reference."""

    device_id: str
    location_id: str
    location_name: str
    room_name: str | None = None
    access: str | None = None
    zone: int | None = None


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """The merged state for a device."""

    hierarchy: HierarchyDevice
    device_status: dict[str, Any] = field(default_factory=dict)
    device_settings: dict[str, Any] = field(default_factory=dict)
    pending_device_settings: dict[str, Any] = field(default_factory=dict)
    device_setup: dict[str, Any] = field(default_factory=dict)
    dehumidifier_status: dict[str, Any] = field(default_factory=dict)
    sensor_hub_status: dict[str, Any] = field(default_factory=dict)
    supported: bool = False
    unsupported_reason: str | None = None

    @property
    def device_id(self) -> str:
        """Return the device identifier."""
        return self.hierarchy.device_id

    @property
    def effective_device_settings(self) -> dict[str, Any]:
        """Return remote settings overlaid with any optimistic local changes."""
        return merge_settings_payload(self.device_settings, self.pending_device_settings)


@dataclass(frozen=True, slots=True)
class SocketState:
    """Connection state for a location websocket."""

    location_id: str
    connected: bool = False
    initial_sync_complete: bool = False
    reconnect_attempt: int = 0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class AprilaireSnapshot:
    """Coordinator snapshot shared with entities."""

    user_id: str
    email: str
    locations: dict[str, HierarchyLocation] = field(default_factory=dict)
    devices: dict[str, DeviceRecord] = field(default_factory=dict)
    socket_states: dict[str, SocketState] = field(default_factory=dict)

    @property
    def supported_device_ids(self) -> tuple[str, ...]:
        """Return all supported device identifiers."""
        return tuple(
            sorted(device_id for device_id, device in self.devices.items() if device.supported)
        )


def empty_snapshot() -> AprilaireSnapshot:
    """Return an empty snapshot."""
    return AprilaireSnapshot(user_id="", email="")
