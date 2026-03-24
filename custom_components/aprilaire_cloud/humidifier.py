"""Humidifier platform for AprilAire Cloud."""

from __future__ import annotations

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


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up the AprilAire humidifier entities."""
    coordinator = entry.runtime_data.coordinator
    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        lambda device_id, device: [AprilaireCloudHumidifierEntity(coordinator, device_id)],
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
    def current_humidity(self) -> float | None:
        """Return the controlling humidity sensor reading."""
        device = self.device
        if device is None:
            return None
        for sensor in device.dehumidifier_status.get("humSensors", []):
            if sensor.get("isControlling"):
                return sensor.get("reading")
        return None

    @property
    def target_humidity(self) -> float | None:
        """Return the humidity setpoint."""
        return self.effective_device_settings.get("dehumidifier", {}).get("humiditySetpoint")

    @property
    def is_on(self) -> bool | None:
        """Return whether the device is enabled."""
        if not self.effective_device_settings:
            return None
        return self.effective_device_settings.get("dehumidifier", {}).get("mode") == "on"

    @property
    def action(self) -> HumidifierAction | None:
        """Return the current operating action."""
        device = self.device
        if device is None:
            return None
        if self.is_on is False:
            return HumidifierAction.OFF
        equipment_status = device.dehumidifier_status.get("equipmentStatus")
        if equipment_status in {"dehumidifying", "defrosting"}:
            return HumidifierAction.DRYING
        if equipment_status in {"inactive", "air-sampling"}:
            return HumidifierAction.IDLE
        return None

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the device on."""
        try:
            await self.coordinator.async_set_mode(self._device_id, True)
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the device off."""
        try:
            await self.coordinator.async_set_mode(self._device_id, False)
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the target humidity."""
        try:
            await self.coordinator.async_set_target_humidity(self._device_id, humidity)
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)

    @property
    def extra_state_attributes(self) -> dict[str, str | float | int | bool | None]:
        """Expose raw AprilAire values that are useful for debugging."""
        attrs = dict(super().extra_state_attributes)
        device = self.device
        if device is not None:
            attrs["equipment_status"] = device.dehumidifier_status.get("equipmentStatus")
            attrs["setpoint_unit"] = PERCENTAGE
        return attrs
