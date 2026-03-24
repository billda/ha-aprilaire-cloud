"""Number platform for AprilAire Cloud."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity

ALERT_LIMIT_DESCRIPTIONS = {
    "highHum": {
        "key": "alert_limit_high_humidity",
        "translation_key": "alert_limit_high_humidity",
        "min": 40,
        "max": 90,
    }
}


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire number entities."""
    coordinator = entry.runtime_data.coordinator
    known_entities: set[tuple[str, str]] = set()

    def _check_devices() -> None:
        entities = []
        for device_id, device in coordinator.data.devices.items():
            if not device.supported:
                continue
            for key in device.device_settings.get("dehumidifier", {}).get("alertLimits", {}):
                if key not in ALERT_LIMIT_DESCRIPTIONS:
                    continue
                entity = AprilaireAlertLimitNumber(coordinator, device_id, key)
                registry_key = (device_id, entity.unique_id)
                if registry_key in known_entities:
                    continue
                known_entities.add(registry_key)
                entities.append(entity)
        if entities:
            async_add_entities(entities)

    _check_devices()
    entry.async_on_unload(coordinator.async_add_listener(_check_devices))


class AprilaireAlertLimitNumber(AprilaireCloudEntity, NumberEntity):
    """Writable alert limit."""

    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, limit_key: str) -> None:
        """Initialize the number."""
        self._limit_key = limit_key
        description = ALERT_LIMIT_DESCRIPTIONS[limit_key]
        self._attr_translation_key = description["translation_key"]
        self._attr_native_min_value = description["min"]
        self._attr_native_max_value = description["max"]
        self._attr_native_step = 1
        super().__init__(coordinator, device_id, description["key"])

    @property
    def native_value(self) -> float | None:
        """Return the current limit value."""
        device = self.device
        if device is None:
            return None
        return device.device_settings.get("dehumidifier", {}).get("alertLimits", {}).get(self._limit_key)

    async def async_set_native_value(self, value: float) -> None:
        """Write a new alert threshold."""
        try:
            await self.coordinator.async_set_alert_limit(self._device_id, self._limit_key, int(value))
        except Exception as err:  # noqa: BLE001
            translation_key = "rate_limited" if err.__class__.__name__.endswith("RateLimitError") else "write_failed"
            placeholders = {"seconds": str(round(getattr(err, "retry_after", 0) or 0))}
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            ) from err

