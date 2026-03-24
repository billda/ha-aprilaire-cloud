"""Sensor platform for AprilAire Cloud."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.entity import EntityCategory

from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import (
    AprilaireCloudEntity,
    sensor_name_from_uid,
    setup_dynamic_platform_entities,
)
from .models import DeviceRecord


@dataclass(frozen=True, kw_only=True)
class AprilaireSensorDescription(SensorEntityDescription):
    """Entity description for a fixed sensor."""

    value_fn: Callable[[DeviceRecord], object | None]
    exists_fn: Callable[[DeviceRecord], bool] = lambda device: True
    enabled_default: bool = True


STATIC_SENSORS: tuple[AprilaireSensorDescription, ...] = (
    AprilaireSensorDescription(
        key="current_humidity",
        translation_key="current_humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: next(
            (
                sensor.get("reading")
                for sensor in device.dehumidifier_status.get("humSensors", [])
                if sensor.get("isControlling")
            ),
            None,
        ),
    ),
    AprilaireSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: next(
            (
                sensor.get("reading")
                for sensor in device.dehumidifier_status.get("tempSensors", [])
                if sensor.get("isControlling")
            ),
            None,
        ),
    ),
    AprilaireSensorDescription(
        key="filter_life",
        translation_key="filter_life",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.dehumidifier_status.get("filterService", {}).get(
            "remaining"
        ),
        exists_fn=lambda device: (
            "remaining" in device.dehumidifier_status.get("filterService", {})
        ),
    ),
    AprilaireSensorDescription(
        key="fan_runtime",
        translation_key="fan_runtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.dehumidifier_status.get("fanTimeHours"),
        exists_fn=lambda device: "fanTimeHours" in device.dehumidifier_status,
        enabled_default=False,
    ),
    AprilaireSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.dehumidifier_status.get("wifiRSSI"),
        exists_fn=lambda device: "wifiRSSI" in device.dehumidifier_status,
        enabled_default=False,
    ),
    AprilaireSensorDescription(
        key="equipment_status",
        translation_key="equipment_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.dehumidifier_status.get("equipmentStatus"),
        exists_fn=lambda device: "equipmentStatus" in device.dehumidifier_status,
        enabled_default=False,
    ),
)


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire sensors."""
    coordinator = entry.runtime_data.coordinator

    def _entities_for_device(device_id: str, device: DeviceRecord):
        for description in STATIC_SENSORS:
            if description.exists_fn(device):
                yield AprilaireStaticSensorEntity(coordinator, device_id, description)

        for temp_sensor in device.dehumidifier_status.get("tempSensors", []):
            if temp_sensor.get("isControlling"):
                continue
            uid = temp_sensor.get("uid")
            if uid is None:
                continue
            yield AprilaireExtraTemperatureSensor(coordinator, device_id, int(uid))

    setup_dynamic_platform_entities(entry, async_add_entities, _entities_for_device)


class AprilaireStaticSensorEntity(AprilaireCloudEntity, SensorEntity):
    """Static sensor backed by a description."""

    entity_description: AprilaireSensorDescription

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
        description: AprilaireSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_entity_registry_enabled_default = description.enabled_default
        self._attr_entity_category = description.entity_category

    @property
    def native_value(self):
        """Return the sensor state."""
        device = self.device
        if device is None:
            return None
        return self.entity_description.value_fn(device)


class AprilaireExtraTemperatureSensor(AprilaireCloudEntity, SensorEntity):
    """A non-controlling temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, uid: int
    ) -> None:
        """Initialize the sensor."""
        self._uid = uid
        super().__init__(coordinator, device_id, f"temperature_{uid}")

    @property
    def name(self) -> str:
        """Return the entity name."""
        device = self.device
        if device is None:
            return f"Temperature {self._uid}"
        return sensor_name_from_uid(device, self._uid, f"Temperature {self._uid}")

    @property
    def native_value(self):
        """Return the current sensor reading."""
        device = self.device
        if device is None:
            return None
        for sensor in device.dehumidifier_status.get("tempSensors", []):
            if sensor.get("uid") == self._uid:
                return sensor.get("reading")
        return None
