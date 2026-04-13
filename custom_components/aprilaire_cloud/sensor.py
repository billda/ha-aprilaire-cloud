"""Sensor platform for AprilAire Cloud."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

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
from .entity import AprilaireCloudEntity, setup_dynamic_platform_entities
from .profiles import NormalizedDehumidifierState, get_profile

DynamicSensorFactory = Callable[
    [AprilaireCloudDataUpdateCoordinator, str, str],
    AprilaireCloudEntity | None,
]


@dataclass(frozen=True, kw_only=True)
class AprilaireSensorDescription(SensorEntityDescription):
    """Entity description for a fixed sensor."""

    value_fn: Callable[[object], object | None]
    enabled_default: bool = True


def _dehumidifier_state(normalized: object) -> NormalizedDehumidifierState:
    """Return dehumidifier-normalized state."""
    return cast(NormalizedDehumidifierState, normalized)


DEHUMIDIFIER_SENSORS: dict[str, AprilaireSensorDescription] = {
    "current_humidity": AprilaireSensorDescription(
        key="current_humidity",
        translation_key="current_humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda normalized: _dehumidifier_state(normalized).current_humidity,
    ),
    "current_temperature": AprilaireSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda normalized: _dehumidifier_state(normalized).current_temperature,
    ),
    "filter_life": AprilaireSensorDescription(
        key="filter_life",
        translation_key="filter_life",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda normalized: _dehumidifier_state(normalized).filter_remaining,
    ),
    "fan_runtime": AprilaireSensorDescription(
        key="fan_runtime",
        translation_key="fan_runtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: _dehumidifier_state(normalized).fan_runtime_hours,
        enabled_default=False,
    ),
    "wifi_signal": AprilaireSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: _dehumidifier_state(normalized).wifi_rssi,
        enabled_default=False,
    ),
    "equipment_status": AprilaireSensorDescription(
        key="equipment_status",
        translation_key="equipment_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: _dehumidifier_state(normalized).equipment_status,
        enabled_default=False,
    ),
}
PROFILE_SENSOR_DESCRIPTIONS: dict[str, dict[str, AprilaireSensorDescription]] = {
    "dehumidifier": DEHUMIDIFIER_SENSORS,
}


def _dehumidifier_dynamic_sensor(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    device_id: str,
    key: str,
) -> AprilaireCloudEntity | None:
    """Create a dehumidifier-owned dynamic sensor."""
    prefix = "temperature_"
    if not key.startswith(prefix):
        return None
    try:
        uid = int(key.removeprefix(prefix))
    except ValueError:
        return None
    return AprilaireDehumidifierExtraTemperatureSensor(coordinator, device_id, uid)


PROFILE_DYNAMIC_SENSOR_FACTORIES: dict[str, DynamicSensorFactory] = {
    "dehumidifier": _dehumidifier_dynamic_sensor,
}


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire sensors."""
    coordinator = entry.runtime_data.coordinator

    def _entities_for_device(device_id: str, device):
        profile = get_profile(coordinator.data.devices[device_id].profile_key)
        if profile is None:
            return
        entity_set = profile.entity_descriptions(coordinator.data.devices[device_id])
        descriptions = PROFILE_SENSOR_DESCRIPTIONS.get(device.profile_key, {})
        for key in entity_set.sensor_keys:
            description = descriptions.get(key)
            if description is not None:
                yield AprilaireStaticSensorEntity(coordinator, device_id, description)

        dynamic_sensor_factory = PROFILE_DYNAMIC_SENSOR_FACTORIES.get(device.profile_key)
        if dynamic_sensor_factory is None:
            return
        for key in entity_set.dynamic_sensor_keys:
            entity = dynamic_sensor_factory(coordinator, device_id, key)
            if entity is not None:
                yield entity

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
        self._attr_entity_category = description.entity_category
        self._attr_entity_registry_enabled_default = description.enabled_default or (
            description.entity_category is EntityCategory.DIAGNOSTIC
            and coordinator.extra_diagnostics_enabled
        )

    @property
    def native_value(self):
        """Return the sensor state."""
        normalized = self.normalized_state
        if normalized is None:
            return None
        return self.entity_description.value_fn(normalized)


class AprilaireDehumidifierExtraTemperatureSensor(AprilaireCloudEntity, SensorEntity):
    """A dehumidifier-owned non-controlling temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, uid: int
    ) -> None:
        """Initialize the sensor."""
        self._uid = uid
        super().__init__(coordinator, device_id, f"temperature_{uid}")
        self._attr_entity_registry_enabled_default = coordinator.extra_diagnostics_enabled

    @property
    def name(self) -> str:
        """Return the entity name."""
        normalized = cast(NormalizedDehumidifierState | None, self.normalized_state)
        if normalized is None:
            return f"Temperature {self._uid}"
        for probe in normalized.extra_temperature_probes:
            if probe.uid == self._uid:
                return probe.name
        return f"Temperature {self._uid}"

    @property
    def native_value(self):
        """Return the current sensor reading."""
        normalized = cast(NormalizedDehumidifierState | None, self.normalized_state)
        if normalized is None:
            return None
        for probe in normalized.extra_temperature_probes:
            if probe.uid == self._uid:
                return probe.reading
        return None
