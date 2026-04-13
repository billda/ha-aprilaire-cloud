"""Humidifier platform for AprilAire Cloud."""

from __future__ import annotations

from typing import cast

from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
)
from homeassistant.const import PERCENTAGE

from .api import AprilaireCloudApiError, AprilaireCloudRateLimitError
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity, raise_ha_write_error, setup_dynamic_platform_entities
from .profiles import NormalizedDehumidifierState, get_profile


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up the AprilAire humidifier entities."""
    coordinator = entry.runtime_data.coordinator
    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        lambda device_id, device: (
            [AprilaireCloudHumidifierEntity(coordinator, device_id)]
            if device.profile_key == "dehumidifier" and get_profile(device.profile_key) is not None
            else []
        ),
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
        if equipment_status in {"dehumidifying", "defrosting"}:
            return HumidifierAction.DRYING
        if equipment_status in {"inactive", "air-sampling"}:
            return HumidifierAction.IDLE
        return None

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the device on."""
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {"dehumidifier": {"mode": "on"}},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the device off."""
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {"dehumidifier": {"mode": "off"}},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the target humidity."""
        try:
            await self.coordinator.async_write_device_settings(
                self._device_id,
                {"dehumidifier": {"humiditySetpoint": humidity}},
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
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
