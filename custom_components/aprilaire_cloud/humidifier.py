"""Humidifier platform for AprilAire Cloud."""

from __future__ import annotations

from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
)
from homeassistant.const import PERCENTAGE
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up the AprilAire humidifier entities."""
    coordinator = entry.runtime_data.coordinator
    known_devices: set[str] = set()

    def _check_devices() -> None:
        current_devices = {
            device_id
            for device_id, device in coordinator.data.devices.items()
            if device.supported
        }
        new_devices = current_devices - known_devices
        if not new_devices:
            return
        known_devices.update(new_devices)
        async_add_entities(
            AprilaireCloudHumidifierEntity(coordinator, device_id)
            for device_id in sorted(new_devices)
        )

    _check_devices()
    entry.async_on_unload(coordinator.async_add_listener(_check_devices))


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
        device = self.device
        if device is None:
            return None
        return device.device_settings.get("dehumidifier", {}).get("humiditySetpoint")

    @property
    def is_on(self) -> bool | None:
        """Return whether the device is enabled."""
        device = self.device
        if device is None:
            return None
        return device.device_settings.get("dehumidifier", {}).get("mode") == "on"

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
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
            ) from err

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the device off."""
        try:
            await self.coordinator.async_set_mode(self._device_id, False)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
            ) from err

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the target humidity."""
        try:
            await self.coordinator.async_set_target_humidity(self._device_id, humidity)
        except Exception as err:  # noqa: BLE001
            translation_key = "rate_limited" if err.__class__.__name__.endswith("RateLimitError") else "write_failed"
            placeholders = {"seconds": str(round(getattr(err, "retry_after", 0) or 0))}
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            ) from err

    @property
    def extra_state_attributes(self) -> dict[str, str | float | int | bool | None]:
        """Expose raw AprilAire values that are useful for debugging."""
        attrs = dict(super().extra_state_attributes)
        device = self.device
        if device is not None:
            attrs["equipment_status"] = device.dehumidifier_status.get("equipmentStatus")
            attrs["setpoint_unit"] = PERCENTAGE
        return attrs
