"""Shared entity helpers for AprilAire Cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .models import DeviceRecord


def sensor_name_from_uid(device: DeviceRecord, uid: int, fallback: str) -> str:
    """Resolve a sensor display name from device settings."""
    for sensor in device.device_settings.get("dehumidifier", {}).get("sensors", []):
        if sensor.get("uid") == uid:
            return sensor.get("dispName", fallback)
    return fallback


class AprilaireCloudEntity(CoordinatorEntity[AprilaireCloudDataUpdateCoordinator]):
    """Base entity for AprilAire Cloud."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}"

    @property
    def device(self) -> DeviceRecord | None:
        """Return the current device record."""
        return self.coordinator.data.devices.get(self._device_id)

    @property
    def available(self) -> bool:
        """Return whether the entity has current device data."""
        device = self.device
        return super().available and device is not None and device.supported

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry metadata."""
        device = self.device
        assert device is not None
        status = device.device_status
        name_parts = [device.hierarchy.location_name]
        if device.hierarchy.room_name:
            name_parts.append(device.hierarchy.room_name)
        if model := status.get("model"):
            name_parts.append(model)

        info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer=MANUFACTURER,
            name=" ".join(name_parts),
            suggested_area=device.hierarchy.room_name,
            model=status.get("model"),
            sw_version=status.get("firmwareRev"),
        )
        if hardware := status.get("hardwareRev"):
            info["hw_version"] = str(hardware)
        return info

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful shared attributes."""
        device = self.device
        if device is None:
            return {}
        return {
            "device_id": device.device_id,
            "location": device.hierarchy.location_name,
            "room": device.hierarchy.room_name,
            "access": device.hierarchy.access,
            "zone": device.hierarchy.zone,
        }

