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

from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import (
    AprilaireCloudEntity,
    DynamicEntityDescriptor,
    raise_ha_write_error,
    setup_dynamic_platform_entities,
)
from .profiles import (
    AprilaireCommandError,
    CommandType,
    DeviceCommand,
    NormalizedThermostatState,
    NormalizedThermostatZoneState,
    SetThermostatFan,
    SetThermostatHold,
    SetThermostatMode,
    SetThermostatSetpoints,
    get_profile,
)
from .vendor import AprilaireCloudApiError, AprilaireCloudRateLimitError

FAN_MODES = ["auto", "on", "circulate"]
PRESET_MODES = ["none", "temporary", "permanent", "vacation"]

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

    def _descriptors_for_device(device_id: str, device):
        profile = get_profile(coordinator.data.devices[device_id].profile_key)
        if profile is None:
            return
        entity_set = profile.entity_descriptions(coordinator.data.devices[device_id])
        for key in entity_set.climate_keys:
            yield DynamicEntityDescriptor(
                unique_id=f"{device_id}_{key}",
                factory=AprilaireThermostatClimateEntity,
                args=(coordinator, device_id, key),
            )

    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        _descriptors_for_device,
    )


def _zone_key_from_entity_key(entity_key: str) -> str:
    """Return PZ1/SZ2/SZ3 from a climate entity key."""
    return entity_key.removeprefix("thermostat_").upper()


def _ha_temperature_unit(unit: str | None, fallback: str) -> str:
    """Return an explicit protocol unit or the HA display fallback."""
    if unit == "C":
        return UnitOfTemperature.CELSIUS
    if unit == "F":
        return UnitOfTemperature.FAHRENHEIT
    return fallback


def _coerce_hvac_mode(value: HVACMode | str | None) -> HVACMode | None:
    """Return an HVACMode for service values."""
    if value is None:
        return None
    if isinstance(value, HVACMode):
        return value
    return HVACMode(value)


class AprilaireThermostatClimateEntity(AprilaireCloudEntity, ClimateEntity):
    """Representation of a supported AprilAire thermostat zone."""

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
        self._attr_translation_key = "thermostat_zone"
        self._attr_translation_placeholders = {"zone": self._zone_key}

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
        return _ha_temperature_unit(
            zone.temperature_unit if zone else None,
            self.coordinator.hass.config.units.temperature_unit,
        )

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Advertise only locally writable thermostat features."""
        device = self.device
        profile = self.profile
        if device is None or profile is None:
            return ClimateEntityFeature(0)
        commands = profile.capabilities(device).commands
        features = ClimateEntityFeature(0)
        setpoints = commands.get(CommandType.THERMOSTAT_SETPOINTS)
        if setpoints is not None and setpoints.writable:
            features |= (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            )
        fan = commands.get(CommandType.THERMOSTAT_FAN)
        if fan is not None and fan.writable:
            features |= ClimateEntityFeature.FAN_MODE
        hold = commands.get(CommandType.THERMOSTAT_HOLD)
        if hold is not None and hold.writable:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    def _allowed_values(self, command_type: CommandType) -> tuple[str, ...]:
        """Return profile-confirmed enum values for this device."""
        device = self.device
        profile = self.profile
        if device is None or profile is None:
            return ()
        capability = profile.capabilities(device).commands.get(command_type)
        return () if capability is None else capability.allowed_values

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return only modes confirmed for this thermostat contract."""
        modes = [
            HA_MODE_BY_RAW_MODE[value]
            for value in self._allowed_values(CommandType.THERMOSTAT_MODE)
            if value in HA_MODE_BY_RAW_MODE and value != "emergency-heat"
        ]
        if modes:
            return modes
        current = self.hvac_mode
        return [current] if current is not None else []

    @property
    def fan_modes(self) -> list[str] | None:
        """Return only fan modes confirmed for this device."""
        values = self._allowed_values(CommandType.THERMOSTAT_FAN)
        return list(values) or None

    @property
    def preset_modes(self) -> list[str] | None:
        """Return only hold modes confirmed for this device."""
        values = self._allowed_values(CommandType.THERMOSTAT_HOLD)
        return list(values) or None

    @property
    def current_temperature(self) -> float | None:
        """Return the current indoor temperature."""
        zone = self._zone
        if zone is None or zone.temperature_unit is None:
            return None
        return zone.current_temperature

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
        """Return the current Home Assistant HVAC action."""
        zone = self._zone
        if zone is None or zone.operating_state is None:
            return None
        return HA_ACTION_BY_STATUS.get(zone.operating_state)

    @property
    def target_temperature(self) -> float | None:
        """Return the single setpoint for heat or cool modes."""
        zone = self._zone
        if zone is None or zone.temperature_unit is None:
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
        if zone is None or zone.temperature_unit is None:
            return None
        return zone.heat_setpoint

    @property
    def target_temperature_high(self) -> float | None:
        """Return the cool setpoint."""
        zone = self._zone
        if zone is None or zone.temperature_unit is None:
            return None
        return zone.cool_setpoint

    @property
    def fan_mode(self) -> str | None:
        """Return the thermostat fan mode."""
        zone = self._zone
        if zone is None or zone.raw_fan not in FAN_MODES:
            return None
        return zone.raw_fan

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
        await self._async_execute(SetThermostatMode(zone=self._zone_key, mode=raw_mode))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set thermostat setpoints."""
        target_mode = _coerce_hvac_mode(kwargs.get(ATTR_HVAC_MODE)) or self.hvac_mode
        temperature = kwargs.get(ATTR_TEMPERATURE)
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        heat: float | None = None
        cool: float | None = None
        if target_mode == HVACMode.HEAT:
            value = temperature if temperature is not None else low
            if value is not None:
                heat = float(value)
        elif target_mode == HVACMode.COOL:
            value = temperature if temperature is not None else high
            if value is not None:
                cool = float(value)
        elif target_mode == HVACMode.HEAT_COOL:
            heat_value = low if low is not None else temperature
            cool_value = high if high is not None else temperature
            if heat_value is not None:
                heat = float(heat_value)
            if cool_value is not None:
                cool = float(cool_value)

        if heat is None and cool is None:
            raise HomeAssistantError("No supported thermostat setpoint was provided")
        await self._async_execute(
            SetThermostatSetpoints(zone=self._zone_key, heat=heat, cool=cool)
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the thermostat fan mode."""
        if fan_mode not in FAN_MODES:
            raise HomeAssistantError(f"Unsupported AprilAire thermostat fan mode: {fan_mode}")
        await self._async_execute(SetThermostatFan(zone=self._zone_key, mode=fan_mode))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the thermostat hold mode."""
        if preset_mode not in PRESET_MODES:
            raise HomeAssistantError(
                f"Unsupported AprilAire thermostat preset mode: {preset_mode}"
            )
        await self._async_execute(SetThermostatHold(zone=self._zone_key, hold=preset_mode))

    async def _async_execute(self, command: DeviceCommand) -> None:
        """Execute a profile-owned thermostat command."""
        try:
            await self.coordinator.async_execute_command(self._device_id, command)
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
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
                "raw_heating_status": zone.heating_status,
                "raw_cooling_status": zone.cooling_status,
                "raw_fan_on": zone.fan_on,
                "operating_state": zone.operating_state,
            }
        )
        return attrs
