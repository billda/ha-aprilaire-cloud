"""Climate platform for AprilAire Cloud thermostats."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError

from .api import AprilaireCloudApiError, AprilaireCloudRateLimitError
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity, raise_ha_write_error, setup_dynamic_platform_entities
from .profiles import (
    THERMOSTAT_ZONE_SETTINGS_KEYS,
    NormalizedThermostatState,
    NormalizedThermostatZoneState,
    get_profile,
)

FAN_MODES = ["auto", "on", "circulate"]
PRESET_MODES = ["none", "temporary", "permanent", "vacation"]
HVAC_MODES = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
SUPPORTED_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.PRESET_MODE
)

RAW_MODE_BY_HA_MODE = {
    HVACMode.OFF: "off",
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.HEAT_COOL: "auto",
}

HA_MODE_BY_RAW_MODE = {
    "off": HVACMode.OFF,
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "auto": HVACMode.HEAT_COOL,
    "emergency-heat": HVACMode.HEAT,
}

HA_ACTION_BY_STATUS = {
    "off": HVACAction.OFF,
    "heating": HVACAction.HEATING,
    "heat": HVACAction.HEATING,
    "aux-heat": HVACAction.HEATING,
    "emergency-heat": HVACAction.HEATING,
    "cooling": HVACAction.COOLING,
    "cool": HVACAction.COOLING,
    "fan": HVACAction.FAN,
    "fan-only": HVACAction.FAN,
    "fan-on": HVACAction.FAN,
    "idle": HVACAction.IDLE,
    "inactive": HVACAction.IDLE,
    "standby": HVACAction.IDLE,
}


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire thermostat climate entities."""
    coordinator = entry.runtime_data.coordinator

    def _entities_for_device(device_id: str, device):
        profile = get_profile(coordinator.data.devices[device_id].profile_key)
        if profile is None:
            return
        entity_set = profile.entity_descriptions(coordinator.data.devices[device_id])
        for key in entity_set.climate_keys:
            yield AprilaireThermostatClimateEntity(coordinator, device_id, key)

    setup_dynamic_platform_entities(entry, async_add_entities, _entities_for_device)


def _zone_key_from_entity_key(entity_key: str) -> str:
    """Return PZ1/SZ2/SZ3 from a climate entity key."""
    return entity_key.removeprefix("thermostat_").upper()


def _ha_temperature_unit(unit: str | None) -> str:
    """Return a Home Assistant temperature unit."""
    return UnitOfTemperature.CELSIUS if unit == "C" else UnitOfTemperature.FAHRENHEIT


def _temperature_for_patch(value: Any) -> int | float:
    """Return an integer temperature when possible for cleaner PATCH payloads."""
    number = float(value)
    if number.is_integer():
        return int(number)
    return number


def _coerce_hvac_mode(value: HVACMode | str | None) -> HVACMode | None:
    """Return an HVACMode for service values."""
    if value is None:
        return None
    if isinstance(value, HVACMode):
        return value
    return HVACMode(value)


class AprilaireThermostatClimateEntity(AprilaireCloudEntity, ClimateEntity):
    """Representation of a supported AprilAire thermostat zone."""

    _attr_supported_features = SUPPORTED_FEATURES
    _attr_hvac_modes = HVAC_MODES
    _attr_fan_modes = FAN_MODES
    _attr_preset_modes = PRESET_MODES
    _attr_target_temperature_step = 1

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
        entity_key: str,
    ) -> None:
        """Initialize the thermostat climate entity."""
        self._entity_key = entity_key
        self._zone_key = _zone_key_from_entity_key(entity_key)
        super().__init__(coordinator, device_id, entity_key)
        self._attr_name = f"{self._zone_key} Thermostat"

    @property
    def _normalized_thermostat(self) -> NormalizedThermostatState | None:
        """Return normalized thermostat state."""
        return cast(NormalizedThermostatState | None, self.normalized_state)

    @property
    def _zone(self) -> NormalizedThermostatZoneState | None:
        """Return normalized state for this thermostat zone."""
        normalized = self._normalized_thermostat
        if normalized is None:
            return None
        return normalized.zones.get(self._zone_key)

    @property
    def available(self) -> bool:
        """Return whether the thermostat zone is currently available."""
        return super().available and self._zone is not None

    @property
    def temperature_unit(self) -> str:
        """Return the thermostat temperature unit."""
        zone = self._zone
        return _ha_temperature_unit(zone.temperature_unit if zone else "F")

    @property
    def min_temp(self) -> float:
        """Return the minimum settable temperature."""
        return 7 if self.temperature_unit == UnitOfTemperature.CELSIUS else 45

    @property
    def max_temp(self) -> float:
        """Return the maximum settable temperature."""
        return 35 if self.temperature_unit == UnitOfTemperature.CELSIUS else 95

    @property
    def current_temperature(self) -> float | None:
        """Return the current indoor temperature."""
        zone = self._zone
        return None if zone is None else zone.current_temperature

    @property
    def current_humidity(self) -> float | None:
        """Return the current indoor humidity."""
        zone = self._zone
        return None if zone is None else zone.current_humidity

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current Home Assistant HVAC mode."""
        zone = self._zone
        if zone is None or zone.raw_mode is None:
            return None
        return HA_MODE_BY_RAW_MODE.get(zone.raw_mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac action state to update the control card UI."""
        # Pull the zone state dataclass object we populated in profiles.py
        zone = self._zone 
        if zone is None or zone.equipment_status is None:
            return HVACAction.IDLE

        status_lower = str(zone.equipment_status).lower()

        # Map custom profile states to the official core Home Assistant HVACAction variables
        if "cooling" in status_lower:
            return HVACAction.COOLING
        if "heating" in status_lower:
            return HVACAction.HEATING
        if "fan" in status_lower:
            return HVACAction.FAN
            
        return HVACAction.IDLE

    @property
    def target_temperature(self) -> float | None:
        """Return the single setpoint for heat or cool modes."""
        zone = self._zone
        if zone is None:
            return None
        if self.hvac_mode == HVACMode.HEAT:
            return zone.heat_setpoint
        if self.hvac_mode == HVACMode.COOL:
            return zone.cool_setpoint
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the heat setpoint."""
        zone = self._zone
        return None if zone is None else zone.heat_setpoint

    @property
    def target_temperature_high(self) -> float | None:
        """Return the cool setpoint."""
        zone = self._zone
        return None if zone is None else zone.cool_setpoint

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan behavior mode."""
        zone = self._zone
        if zone is None or zone.raw_fan is None:
            return "auto"
            
        # Returns clean matching lowercase text string tokens (e.g., "on", "auto")
        return str(zone.raw_fan).lower()

    @property
    def fan_modes(self) -> list[str]:
        """Return the list of selectable fan operation choices dynamically from the profile dataclass."""
        zone = self._zone
        if zone is not None and hasattr(zone, "allowed_fan_modes"):
            return zone.allowed_fan_modes
            
        # Hardcoded safe fallback baseline if dataclass initialization hasn't occurred yet
        return ["auto", "on"]

    @property
    def preset_mode(self) -> str | None:
        """Return the thermostat hold mode."""
        zone = self._zone
        if zone is None or zone.raw_hold_type not in PRESET_MODES:
            return None
        return zone.raw_hold_type

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the thermostat HVAC mode."""
        raw_mode = RAW_MODE_BY_HA_MODE.get(hvac_mode)
        if raw_mode is None:
            raise HomeAssistantError(f"Unsupported AprilAire thermostat mode: {hvac_mode}")
        await self._async_write_zone_settings({"mode": raw_mode})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set thermostat setpoints with explicit Fahrenheit-to-Celsius API conversion."""
        target_mode = _coerce_hvac_mode(kwargs.get(ATTR_HVAC_MODE)) or self.hvac_mode
        temperature = kwargs.get(ATTR_TEMPERATURE)
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        zone = self._zone
        if zone is None:
            raise HomeAssistantError("Thermostat zone data is temporarily unavailable")
            
        current_heat = zone.heat_setpoint
        current_cool = zone.cool_setpoint

        # 1. Determine target values based on user input
        new_heat = low if low is not None else (temperature if target_mode == HVACMode.HEAT else current_heat)
        new_cool = high if high is not None else (temperature if target_mode == HVACMode.COOL else current_cool)

        # 2. Enforce the strict 3-degree deadband guard bands - API seems to reject changes otherwise
        is_fahrenheit = self.temperature_unit == UnitOfTemperature.FAHRENHEIT
        buffer = 3.0 if is_fahrenheit else 1.66

        if target_mode == HVACMode.COOL or (high is not None):
            if new_cool - new_heat < buffer:
                new_heat = new_cool - buffer
        elif target_mode == HVACMode.HEAT or (low is not None):
            if new_cool - new_heat < buffer:
                new_cool = new_heat + buffer
        elif target_mode == HVACMode.HEAT_COOL:
            if new_cool - new_heat < buffer:
                new_cool = new_heat + buffer

        # 3. CRITICAL PRODUCTION PATCH: Convert Fahrenheit down to native Celsius floats
        # The Aprilaire cloud API endpoints only accept Celsius constraints values.
        if is_fahrenheit:
            final_heat = round((new_heat - 32) * 5 / 9, 2)
            final_cool = round((new_cool - 32) * 5 / 9, 2)
        else:
            final_heat = round(new_heat, 2)
            final_cool = round(new_cool, 2)

        # 4. Package parameters into the flat dictionary payload format expected by existing code
        settings_payload: dict[str, int | float] = {
            "heat": _temperature_for_patch(final_heat),
            "cool": _temperature_for_patch(final_cool)
        }

        # 5. Transmit the payload through the internal structural helper loop
        await self._async_write_zone_settings(settings_payload)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode for the thermostat zone."""
        # 1. Standardize the incoming Home Assistant selection (e.g., "on", "auto", "diffuse")
        fan_mode_lower = str(fan_mode).lower()

        # 2. Map standard HA string variables to the specific tokens your Aprilaire API requires
        # Aprilaire hardware typically expects simple tokens like "on" or "auto"
        if "auto" in fan_mode_lower:
            target_fan = "auto"
        elif "on" in fan_mode_lower:
            target_fan = "on"
        elif "circ" in fan_mode_lower or "continuous" in fan_mode_lower:
            target_fan = "circ"  # Standard vendor shorthand for circulation modes
        else:
            target_fan = fan_mode_lower

        # 3. Package the payload into the flat dictionary structure expected existing code
        settings_payload = {
            "fan": target_fan
        }

        # 4. Transmit the packet through the internal structural helper loop
        await self._async_write_zone_settings(settings_payload)

    async def _async_write_zone_settings(self, settings: dict[str, Any]) -> None:
        """Write settings for this thermostat zone."""
        zone = self._zone
        settings_key = (
            zone.settings_key
            if zone is not None
            else THERMOSTAT_ZONE_SETTINGS_KEYS[self._zone_key]
        )
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {settings_key: settings},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose raw AprilAire thermostat values useful for beta diagnostics."""
        attrs = dict(super().extra_state_attributes)
        zone = self._zone
        if zone is None:
            return attrs
        attrs.update(
            {
                "thermostat_zone": zone.zone_key,
                "settings_key": zone.settings_key,
                "raw_hvac_mode": zone.raw_mode,
                "raw_fan_mode": zone.raw_fan,
                "raw_hold_type": zone.raw_hold_type,
                "raw_equipment_status": zone.equipment_status,
            }
        )
        return attrs
