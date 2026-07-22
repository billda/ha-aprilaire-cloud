"""Humidifier platform for AprilAire Cloud."""

from __future__ import annotations

from typing import cast

from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
)
from homeassistant.const import PERCENTAGE

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
    NormalizedAttachedHumidifierState,
    NormalizedDehumidifierState,
    NormalizedThermostatState,
    SetAttachedHumidifierPower,
    SetAttachedHumidifierTarget,
    SetDehumidifierPower,
    SetDehumidifierTarget,
    get_profile,
)
from .vendor import AprilaireCloudApiError, AprilaireCloudRateLimitError


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up the AprilAire humidifier entities."""
    coordinator = entry.runtime_data.coordinator

    def _descriptors_for_device(device_id: str, device):
        profile = get_profile(device.profile_key)
        if profile is None:
            return
        for key in profile.entity_descriptions(device).humidifier_keys:
            if key == "dehumidifier":
                yield DynamicEntityDescriptor(
                    unique_id=f"{device_id}_dehumidifier",
                    factory=AprilaireCloudHumidifierEntity,
                    args=(coordinator, device_id),
                )
            elif key == "attached_humidifier":
                yield DynamicEntityDescriptor(
                    unique_id=f"{device_id}_attached_humidifier",
                    factory=AprilaireAttachedHumidifierEntity,
                    args=(coordinator, device_id),
                )

    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        _descriptors_for_device,
    )


class AprilaireCloudHumidifierEntity(AprilaireCloudEntity, HumidifierEntity):
    """Representation of a supported AprilAire dehumidifier."""

    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_translation_key = "dehumidifier"
    _attr_name = None
    _attr_min_humidity = 40
    _attr_max_humidity = 80
    _attr_target_humidity_step = 1

    def __init__(self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str) -> None:
        """Initialize the primary entity."""
        super().__init__(coordinator, device_id, "dehumidifier")

    @property
    def _normalized_dehumidifier(self) -> NormalizedDehumidifierState | None:
        """Return normalized dehumidifier state."""
        return cast(NormalizedDehumidifierState | None, self.normalized_state)

    @property
    def current_humidity(self) -> float | None:
        """Return the controlling humidity sensor reading."""
        normalized = self._normalized_dehumidifier
        if normalized is None:
            return None
        return normalized.current_humidity

    @property
    def target_humidity(self) -> float | None:
        """Return the humidity setpoint."""
        normalized = self._normalized_dehumidifier
        if normalized is None:
            return None
        return normalized.target_humidity

    @property
    def is_on(self) -> bool | None:
        """Return whether the device is enabled."""
        normalized = self._normalized_dehumidifier
        if normalized is None:
            return None
        return normalized.mode == "on"

    @property
    def action(self) -> HumidifierAction | None:
        """Return the current operating action."""
        normalized = self._normalized_dehumidifier
        if normalized is None:
            return None
        if self.is_on is False:
            return HumidifierAction.OFF
        equipment_status = normalized.equipment_status
        if equipment_status in {"active", "dehumidifying", "defrosting"}:
            return HumidifierAction.DRYING
        if equipment_status in {"inactive", "air-sampling"}:
            return HumidifierAction.IDLE
        return None

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the device on."""
        try:
            await self.coordinator.async_execute_command(
                self._device_id, SetDehumidifierPower(enabled=True)
            )
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
            raise_ha_write_error(err)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the device off."""
        try:
            await self.coordinator.async_execute_command(
                self._device_id, SetDehumidifierPower(enabled=False)
            )
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
            raise_ha_write_error(err)

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the target humidity."""
        try:
            await self.coordinator.async_execute_command(
                self._device_id, SetDehumidifierTarget(humidity=humidity)
            )
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
            raise_ha_write_error(err)

    @property
    def extra_state_attributes(self) -> dict[str, str | float | int | bool | None]:
        """Expose raw AprilAire values that are useful for debugging."""
        attrs = dict(super().extra_state_attributes)
        normalized = self._normalized_dehumidifier
        if normalized is not None:
            attrs["equipment_status"] = normalized.equipment_status
            attrs["setpoint_unit"] = PERCENTAGE
        return attrs


class AprilaireAttachedHumidifierEntity(AprilaireCloudEntity, HumidifierEntity):
    """A central humidifier explicitly installed on a thermostat."""

    _attr_device_class = HumidifierDeviceClass.HUMIDIFIER
    _attr_translation_key = "attached_humidifier"
    _attr_target_humidity_step = 1

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        """Initialize the global attached humidifier entity."""
        super().__init__(coordinator, device_id, "attached_humidifier")
        device = coordinator.data.devices.get(device_id)
        profile = get_profile(device.profile_key) if device is not None else None
        capability = (
            profile.capabilities(device).commands.get(
                CommandType.ATTACHED_HUMIDIFIER_TARGET
            )
            if profile is not None and device is not None
            else None
        )
        if capability is not None and capability.maximum is not None:
            self._attr_max_humidity = capability.maximum

    @property
    def _normalized_humidifier(self) -> NormalizedAttachedHumidifierState | None:
        """Return the normalized global humidifier state."""
        normalized = cast(NormalizedThermostatState | None, self.normalized_state)
        return None if normalized is None else normalized.attached_humidifier

    @property
    def current_humidity(self) -> float | None:
        """Return explicit humidity or the sole thermostat zone's reading."""
        state = self._normalized_humidifier
        return None if state is None else state.current_humidity

    @property
    def target_humidity(self) -> float | None:
        """Return the configured target only when it is reported."""
        state = self._normalized_humidifier
        return None if state is None else state.target_humidity

    @property
    def is_on(self) -> bool | None:
        """Return the reported global humidifier mode."""
        state = self._normalized_humidifier
        if state is None or state.mode not in {"on", "off"}:
            return None
        return state.mode == "on"

    @property
    def action(self) -> HumidifierAction | None:
        """Map only understood attached-humidifier statuses."""
        state = self._normalized_humidifier
        if state is None:
            return None
        if self.is_on is False:
            return HumidifierAction.OFF
        if state.equipment_status in {"active", "humidifying"}:
            return HumidifierAction.HUMIDIFYING
        if state.equipment_status in {"idle", "inactive"}:
            return HumidifierAction.IDLE
        return None

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the attached humidifier."""
        await self._async_execute(SetAttachedHumidifierPower(enabled=True))

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the attached humidifier."""
        await self._async_execute(SetAttachedHumidifierPower(enabled=False))

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the attached humidifier target."""
        await self._async_execute(SetAttachedHumidifierTarget(humidity=humidity))

    async def _async_execute(
        self,
        command: SetAttachedHumidifierPower | SetAttachedHumidifierTarget,
    ) -> None:
        """Execute one profile-owned attached-humidifier command."""
        try:
            await self.coordinator.async_execute_command(self._device_id, command)
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
            raise_ha_write_error(err)

    @property
    def extra_state_attributes(self) -> dict[str, str | int | bool | None]:
        """Expose understood service state without fabricating missing values."""
        attrs = dict(super().extra_state_attributes)
        state = self._normalized_humidifier
        if state is not None:
            attrs.update(
                {
                    "equipment_status": state.equipment_status,
                    "water_panel_remaining": state.water_panel_remaining,
                    "water_panel_needs_service": state.water_panel_needs_service,
                }
            )
        return attrs
