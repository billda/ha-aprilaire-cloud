"""Number platform for AprilAire Cloud."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import EntityCategory

from .api import AprilaireCloudApiError, AprilaireCloudRateLimitError
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import (
    AprilaireCloudEntity,
    raise_ha_write_error,
    setup_dynamic_platform_entities,
)
from .profiles import get_profile

ALERT_LIMIT_DESCRIPTIONS: dict[str, dict[str, str | float]] = {
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
        profile = get_profile(coordinator.data.devices[device_id].profile_key)
        if profile is None:
            return
        entity_set = profile.entity_descriptions(coordinator.data.devices[device_id])
        for key in entity_set.number_keys:
            if key in ALERT_LIMIT_DESCRIPTIONS:
                yield AprilaireAlertLimitNumber(coordinator, device_id, key)

    setup_dynamic_platform_entities(entry, async_add_entities, _entities_for_device)


class AprilaireAlertLimitNumber(AprilaireCloudEntity, NumberEntity):
    """Writable alert limit."""

    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, limit_key: str
    ) -> None:
        """Initialize the number."""
        self._limit_key = limit_key
        description = ALERT_LIMIT_DESCRIPTIONS[limit_key]
        self._attr_translation_key = str(description["translation_key"])
        self._attr_native_min_value = float(description["min"])
        self._attr_native_max_value = float(description["max"])
        self._attr_native_step = 1
        super().__init__(coordinator, device_id, str(description["key"]))

    @property
    def native_value(self) -> float | None:
        """Return the current limit value."""
        normalized = self.normalized_device
        if normalized is None:
            return None
        return normalized.alert_limits.get(self._limit_key)

    async def async_set_native_value(self, value: float) -> None:
        """Write a new alert threshold."""
        try:
            await self.coordinator.async_set_alert_limit(
                self._device_id, self._limit_key, int(value)
            )
        except (AprilaireCloudRateLimitError, AprilaireCloudApiError) as err:
            raise_ha_write_error(err)
