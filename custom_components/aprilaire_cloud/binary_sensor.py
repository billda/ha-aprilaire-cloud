"""Binary sensor platform for AprilAire Cloud."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.entity import EntityCategory

from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity
from .models import DeviceRecord


@dataclass(frozen=True, kw_only=True)
class AprilaireBinarySensorDescription(BinarySensorEntityDescription):
    """Description for an AprilAire binary sensor."""

    value_fn: Callable[[DeviceRecord], bool | None]
    exists_fn: Callable[[DeviceRecord], bool] = lambda device: True
    enabled_default: bool = True


BINARY_SENSORS: tuple[AprilaireBinarySensorDescription, ...] = (
    AprilaireBinarySensorDescription(
        key="filter_service",
        translation_key="filter_service",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda device: device.dehumidifier_status.get("filterService", {}).get("needsService"),
        exists_fn=lambda device: "needsService" in device.dehumidifier_status.get("filterService", {}),
    ),
    AprilaireBinarySensorDescription(
        key="alert_high_humidity",
        translation_key="alert_high_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda device: device.dehumidifier_status.get("alerts", {}).get("highHum"),
        exists_fn=lambda device: "highHum" in device.dehumidifier_status.get("alerts", {}),
    ),
    AprilaireBinarySensorDescription(
        key="alert_low_humidity",
        translation_key="alert_low_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda device: device.dehumidifier_status.get("alerts", {}).get("lowHum"),
        exists_fn=lambda device: "lowHum" in device.dehumidifier_status.get("alerts", {}),
    ),
    AprilaireBinarySensorDescription(
        key="alert_high_temperature",
        translation_key="alert_high_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda device: device.dehumidifier_status.get("alerts", {}).get("highTemp"),
        exists_fn=lambda device: "highTemp" in device.dehumidifier_status.get("alerts", {}),
    ),
    AprilaireBinarySensorDescription(
        key="alert_low_temperature",
        translation_key="alert_low_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda device: device.dehumidifier_status.get("alerts", {}).get("lowTemp"),
        exists_fn=lambda device: "lowTemp" in device.dehumidifier_status.get("alerts", {}),
    ),
    AprilaireBinarySensorDescription(
        key="compressor",
        translation_key="compressor",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.dehumidifier_status.get("isCompOn"),
        exists_fn=lambda device: "isCompOn" in device.dehumidifier_status,
        enabled_default=False,
    ),
    AprilaireBinarySensorDescription(
        key="dehumidifier_fan",
        translation_key="dehumidifier_fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.dehumidifier_status.get("isDehumFanOn"),
        exists_fn=lambda device: "isDehumFanOn" in device.dehumidifier_status,
        enabled_default=False,
    ),
    AprilaireBinarySensorDescription(
        key="hvac_fan",
        translation_key="hvac_fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.dehumidifier_status.get("isHvacFanOn"),
        exists_fn=lambda device: "isHvacFanOn" in device.dehumidifier_status,
        enabled_default=False,
    ),
)


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire binary sensors."""
    coordinator = entry.runtime_data.coordinator
    known_entities: set[tuple[str, str]] = set()

    def _check_devices() -> None:
        entities = []
        for device_id, device in coordinator.data.devices.items():
            if not device.supported:
                continue
            for description in BINARY_SENSORS:
                if not description.exists_fn(device):
                    continue
                entity = AprilaireBinarySensorEntity(coordinator, device_id, description)
                key = (device_id, entity.unique_id)
                if key in known_entities:
                    continue
                known_entities.add(key)
                entities.append(entity)
        if entities:
            async_add_entities(entities)

    _check_devices()
    entry.async_on_unload(coordinator.async_add_listener(_check_devices))


class AprilaireBinarySensorEntity(AprilaireCloudEntity, BinarySensorEntity):
    """An AprilAire binary sensor."""

    entity_description: AprilaireBinarySensorDescription

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
        description: AprilaireBinarySensorDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_entity_category = description.entity_category
        self._attr_entity_registry_enabled_default = description.enabled_default

    @property
    def is_on(self) -> bool | None:
        """Return whether the binary sensor is on."""
        device = self.device
        if device is None:
            return None
        return self.entity_description.value_fn(device)

