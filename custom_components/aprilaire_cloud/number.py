"""Number platform for AprilAire Cloud."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE

from .api import AprilaireCloudApiError, AprilaireCloudRateLimitError
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import (
    AprilaireCloudEntity,
    raise_ha_write_error,
    setup_dynamic_platform_entities,
)

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

    def _entities_for_device(device_id: str, device):
        alert_limits = device.effective_device_settings.get("dehumidifier", {}).get(
            "alertLimits",
            {},
        )
        for key in alert_limits:
            if key in ALERT_LIMIT_DESCRIPTIONS:
                yield AprilaireAlertLimitNumber(coordinator, device_id, key)

    setup_dynamic_platform_entities(entry, async_add_entities, _entities_for_device)


class AprilaireAlertLimitNumber(AprilaireCloudEntity, NumberEntity):
    """Writable alert limit."""

    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, limit_key: str
    ) -> None:
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
        return (
            self.effective_device_settings.get("dehumidifier", {})
            .get("alertLimits", {})
            .get(self._limit_key)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Write a new alert threshold."""
        try:
            await self.coordinator.async_set_alert_limit(
                self._device_id, self._limit_key, int(value)
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)
