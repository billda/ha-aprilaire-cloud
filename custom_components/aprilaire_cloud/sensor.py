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
from .profiles import (
    NormalizedDehumidifierState,
    NormalizedThermostatState,
    NormalizedThermostatZoneState,
    get_profile,
)

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


THERMOSTAT_ZONE_SENSOR_NAMES = {
    "indoor_temperature": "Indoor temperature",
    "indoor_humidity": "Indoor humidity",
    "outdoor_temperature": "Outdoor temperature",
    "outdoor_humidity": "Outdoor humidity",
    "equipment_status": "Equipment status",
    "hvac_service_remaining": "HVAC service remaining",
    "water_panel_life": "Humidifier water panel life",
}

THERMOSTAT_IAQ_SENSOR_NAMES = {
    "status": "Status",
    "service_remaining": "Service remaining",
}


def _thermostat_dynamic_sensor(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    device_id: str,
    key: str,
) -> AprilaireCloudEntity | None:
    """Create a thermostat-owned dynamic sensor with multi-word key support."""
    thermostat_prefix = "thermostat_"
    if key.startswith(thermostat_prefix):
        remainder = key.removeprefix(thermostat_prefix)
        
        # Safe extraction: Split into maximum 2 parts (zone, metric payload)
        parts = remainder.split("_", 1)
        if len(parts) != 2:
            return None
            
        # FIXED: Use explicit indexing to extract the string values cleanly
        raw_zone, metric = parts[0], parts[1]
        zone_key = raw_zone.upper()
        
        # Production check: ensure it aligns with our allowed whitelist matrix keys
        if metric not in THERMOSTAT_ZONE_SENSOR_NAMES:
            return None

        # Resolve potential dictionary key capitalization mismatch strings safely
        device_data = coordinator.data.devices.get(device_id)
        if device_data and (profile := get_profile(device_data.profile_key)):
            normalized = profile.normalize(device_data)
            if normalized and normalized.zones:
                if raw_zone.lower() in normalized.zones:
                    zone_key = raw_zone.lower()
                elif raw_zone.upper() in normalized.zones:
                    zone_key = raw_zone.upper()
            
        return AprilaireThermostatZoneSensor(coordinator, device_id, zone_key, metric, key)

    # --- KEEP THE AUTHOR'S IAQ CODE EXACTLY AS IT WAS ---
    iaq_prefix = "iaq_"
    if key.startswith(iaq_prefix):
        remainder = key.removeprefix(iaq_prefix)
        for metric in THERMOSTAT_IAQ_SENSOR_NAMES:
            suffix = f"_{metric}"
            if remainder.endswith(suffix):
                kind = remainder[: -len(suffix)]
                return AprilaireThermostatIAQSensor(coordinator, device_id, kind, metric, key)

    return None


PROFILE_DYNAMIC_SENSOR_FACTORIES["thermostat"] = _thermostat_dynamic_sensor


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire sensors cleanly."""
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

        # Fall back to checking by partial string match if your device profile key uses a variant string
        dynamic_sensor_factory = PROFILE_DYNAMIC_SENSOR_FACTORIES.get(device.profile_key)
        if dynamic_sensor_factory is None and "thermostat" in str(device.profile_key).lower():
            dynamic_sensor_factory = PROFILE_DYNAMIC_SENSOR_FACTORIES.get("thermostat")

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


def _thermostat_state(normalized: object | None) -> NormalizedThermostatState | None:
    """Return thermostat-normalized state."""
    return cast(NormalizedThermostatState | None, normalized)


def _thermostat_temperature_unit(zone: NormalizedThermostatZoneState | None) -> str:
    """Return a Home Assistant temperature unit for a thermostat zone."""
    if zone and zone.temperature_unit == "C":
        return UnitOfTemperature.CELSIUS
    return UnitOfTemperature.FAHRENHEIT


class AprilaireThermostatZoneSensor(AprilaireCloudEntity, SensorEntity):
    """A thermostat-owned zone sensor."""

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
        zone_key: str,
        metric: str,
        key: str,
    ) -> None:
        """Initialize the sensor."""
        self._zone_key = zone_key
        self._metric = metric
        super().__init__(coordinator, device_id, key)
        if metric == "indoor_temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_registry_enabled_default = True
        elif metric in {"indoor_humidity", "outdoor_humidity"}:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif metric == "outdoor_temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif metric == "equipment_status":
            self._attr_entity_registry_enabled_default = True
        elif metric == "hvac_service_remaining":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = coordinator.extra_diagnostics_enabled
        elif metric == "water_panel_life":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = True  
            self._attr_translation_key = "water_panel_life"
        else:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = coordinator.extra_diagnostics_enabled

    @property
    def name(self) -> str:
        """Return the entity name."""
        return f"{self._zone_key} {THERMOSTAT_ZONE_SENSOR_NAMES[self._metric]}"

    @property
    def _zone(self) -> NormalizedThermostatZoneState | None:
        """Return normalized thermostat zone state."""
        normalized = _thermostat_state(self.normalized_state)
        if normalized is None:
            return None
        return normalized.zones.get(self._zone_key)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit for thermostat temperature sensors."""
        if self._metric in {"indoor_temperature", "outdoor_temperature"}:
            return _thermostat_temperature_unit(self._zone)
        return getattr(self, "_attr_native_unit_of_measurement", None)

    @property
    def native_value(self):
        """Return the current sensor reading."""
        zone = self._zone
        if zone is None:
            return None
        if self._metric == "indoor_temperature":
            return zone.current_temperature
        if self._metric == "indoor_humidity":
            return zone.current_humidity
        if self._metric == "outdoor_temperature":
            return zone.outdoor_temperature
        if self._metric == "outdoor_humidity":
            return zone.outdoor_humidity
        if self._metric == "equipment_status":
            return zone.equipment_status if zone.equipment_status else "Idle"
        if self._metric == "hvac_service_remaining":
            return zone.hvac_service_remaining
        if self._metric == "water_panel_life":
            return zone.water_panel_remaining
        return None


class AprilaireThermostatIAQSensor(AprilaireCloudEntity, SensorEntity):
    """A read-only thermostat-owned IAQ status sensor."""

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
        kind: str,
        metric: str,
        key: str,
    ) -> None:
        """Initialize the sensor."""
        self._kind = kind
        self._metric = metric
        super().__init__(coordinator, device_id, key)
        if metric == "service_remaining":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def name(self) -> str:
        """Return the entity name."""
        kind_name = self._kind.replace("freshair", "fresh air").replace(
            "aircleaning", "air cleaning"
        )
        return f"{kind_name.title()} {THERMOSTAT_IAQ_SENSOR_NAMES[self._metric]}"

    @property
    def native_value(self):
        """Return the IAQ sensor value."""
        normalized = _thermostat_state(self.normalized_state)
        if normalized is None or self._kind not in normalized.iaq:
            return None
        iaq_state = normalized.iaq[self._kind]
        if self._metric == "status":
            return iaq_state.status
        if self._metric == "service_remaining":
            return iaq_state.service_remaining
        return None
