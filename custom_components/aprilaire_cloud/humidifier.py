"""Humidifier platform for AprilAire Cloud."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.const import PERCENTAGE

from .api import AprilaireCloudApiError, AprilaireCloudRateLimitError
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity, raise_ha_write_error, setup_dynamic_platform_entities
from .profiles import NormalizedDehumidifierState, NormalizedThermostatState, get_profile


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up the AprilAire humidifier and dehumidifier entities."""
    coordinator = entry.runtime_data.coordinator
    
    def _get_entities(device_id: str, device) -> list[HumidifierEntity]:
        # Gracefully handle missing profile definitions
        if not device or not getattr(device, "profile_key", None):
            return []

        entities: list[HumidifierEntity] = []

        # 1. Handle Standalone Dehumidifiers
        if device.profile_key == "dehumidifier":
            entities.append(AprilaireCloudDehumidifierEntity(coordinator, device_id))
            
        # 2. Handle Thermostats with attached central humidifiers
        # Change "thermostat" below to match your integration's literal profile_key for thermostats
        elif "thermostat" in str(device.profile_key).lower():
            entities.append(AprilaireCloudCentralHumidifierEntity(coordinator, device_id))
            
        return entities

    setup_dynamic_platform_entities(entry, async_add_entities, _get_entities)


class BaseAprilaireCloudHumidifierEntity(AprilaireCloudEntity, HumidifierEntity):
    """Abstract base to manage common shared state updates for AprilAire moisture controls."""

    _attr_name = None
    _attr_target_humidity_step = 1

    # Concrete variations defined by specialized child subclasses
    _api_section_key: str

    def __init__(self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, entity_key: str) -> None:
        """Initialize the core moisture balance tracker."""
        super().__init__(coordinator, device_id, entity_key)

    @property
    def is_on(self) -> bool | None:
        """Return whether the target system is toggled on."""
        raise NotImplementedError

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the system feature on."""
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {self._api_section_key: {"mode": "on"}},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the system feature off."""
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {self._api_section_key: {"mode": "off"}},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    async def async_set_humidity(self, humidity: int) -> None:
        """Adjust target percentage limits safely using child bounds rules."""
        target = max(self._attr_min_humidity, min(humidity, self._attr_max_humidity))
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {self._api_section_key: {"humiditySetpoint": int(target)}},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    # Added to expose Humidifier operational status to HA as part of the Humidifier entity
    @property
    def supported_features(self) -> HumidifierEntityFeature:
        """Return the list of supported features."""
        # Cleanly instantiate an empty feature flag map to bypass the MODES requirement check
        return HumidifierEntityFeature(0)

class AprilaireCloudDehumidifierEntity(BaseAprilaireCloudHumidifierEntity):
    """Representation of a supported AprilAire standalone dehumidifier."""

    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_translation_key = "dehumidifier"
    _attr_min_humidity = 40
    _attr_max_humidity = 80
    _api_section_key = "dehumidifier"

    def __init__(self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str) -> None:
        """Initialize the standalone dehumidifier entity instance."""
        super().__init__(coordinator, device_id, "dehumidifier")

    @property
    def _normalized_dehumidifier(self) -> NormalizedDehumidifierState | None:
        """Return normalized standalone profile attributes."""
        return cast(NormalizedDehumidifierState | None, self.normalized_state)

    @property
    def current_humidity(self) -> float | None:
        """Return the controlling humidity sensor reading."""
        if normalized := self._normalized_dehumidifier:
            return normalized.current_humidity
        return None

    @property
    def target_humidity(self) -> float | None:
        """Return the humidity setpoint."""
        if normalized := self._normalized_dehumidifier:
            return normalized.target_humidity
        return None

    @property
    def is_on(self) -> bool | None:
        """Return true if active state equals on."""
        if normalized := self._normalized_dehumidifier:
            return normalized.mode == "on"
        return None

    @property
    def action(self) -> HumidifierAction | None:
        """Return the current operating action state."""
        if normalized := self._normalized_dehumidifier:
            if self.is_on is False:
                return HumidifierAction.OFF
            equipment_status = normalized.equipment_status
            if equipment_status in {"dehumidifying", "defrosting"}:
                return HumidifierAction.DRYING
            if equipment_status in {"inactive", "air-sampling"}:
                return HumidifierAction.IDLE
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose raw data properties for historical debugging."""
        attrs = dict(super().extra_state_attributes)
        if normalized := self._normalized_dehumidifier:
            attrs["equipment_status"] = normalized.equipment_status
            attrs["setpoint_unit"] = PERCENTAGE
        return attrs


class AprilaireCloudCentralHumidifierEntity(BaseAprilaireCloudHumidifierEntity):
    """Representation of a central humidifier appended to a thermostat zone state."""

    _attr_device_class = HumidifierDeviceClass.HUMIDIFIER
    _attr_translation_key = "humidifier"
    _attr_min_humidity = 10
    _attr_max_humidity = 50
    _api_section_key = "humidifier"

    def __init__(self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str) -> None:
        """Initialize the thermostat-attached central module."""
        super().__init__(coordinator, device_id, "humidifier")

    @property
    def _zone_state(self) -> Any | None:
        """Isolate data from the first active payload zone class definition."""
        state = cast(NormalizedThermostatState | None, self.normalized_state)
        if state and state.zones:
            first_zone_key = next(iter(state.zones))
            return state.zones[first_zone_key]
        return None

    @property
    def current_humidity(self) -> float | None:
        """Return current ambient humidity tracking variables via the home zone."""
        if zone := self._zone_state:
            return zone.current_humidity
        return None

    @property
    def target_humidity(self) -> float | None:
        """Return slider target value."""
        if zone := self._zone_state:
            return zone.humidifier_setpoint
        return None

    @property
    def is_on(self) -> bool | None:
        """Evaluate if master switch variable status equals on."""
        if zone := self._zone_state:
            return zone.humidifier_mode == "on"
        return None

    @property
    def action(self) -> HumidifierAction | None:
        """Return the running operational state."""
        if self.is_on is False:
            return HumidifierAction.OFF

        zone = self._zone_state
        if not zone or zone.humidifier_status is None:
            return HumidifierAction.IDLE

        # Normalize tracking variables safely
        status = str(zone.humidifier_status).strip().lower()
        if status == "active":
            return HumidifierAction.HUMIDIFYING

        return HumidifierAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose tracking fields natively alongside main cards."""
        attrs = dict(super().extra_state_attributes)
        if zone := self._zone_state:
            attrs["equipment_status"] = zone.humidifier_status
            attrs["setpoint_unit"] = PERCENTAGE
        return attrs
