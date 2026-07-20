"""Number platform for AprilAire Cloud."""

from __future__ import annotations

from typing import cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import EntityCategory

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
    NormalizedDehumidifierState,
    SetHighHumidityAlert,
    get_profile,
)
from .vendor import AprilaireCloudApiError, AprilaireCloudRateLimitError

ALERT_LIMIT_DESCRIPTIONS: dict[str, dict[str, str | float]] = {
    "high_humidity": {
        "key": "alert_limit_high_humidity",
        "translation_key": "alert_limit_high_humidity",
        "min": 40,
        "max": 90,
    }
}


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire number entities."""
    coordinator = entry.runtime_data.coordinator

    def _descriptors_for_device(device_id: str, device):
        profile = get_profile(coordinator.data.devices[device_id].profile_key)
        if profile is None:
            return
        entity_set = profile.entity_descriptions(coordinator.data.devices[device_id])
        for key in entity_set.number_keys:
            if key in ALERT_LIMIT_DESCRIPTIONS:
                entity_key = str(ALERT_LIMIT_DESCRIPTIONS[key]["key"])
                yield DynamicEntityDescriptor(
                    unique_id=f"{device_id}_{entity_key}",
                    factory=AprilaireAlertLimitNumber,
                    args=(coordinator, device_id, key),
                )

    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        _descriptors_for_device,
    )


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
        normalized = cast(NormalizedDehumidifierState | None, self.normalized_state)
        if normalized is None:
            return None
        if self._limit_key == "high_humidity":
            return normalized.high_humidity_alert_limit
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Write a new alert threshold."""
        try:
            await self.coordinator.async_execute_command(
                self._device_id, SetHighHumidityAlert(humidity=int(value))
            )
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
            raise_ha_write_error(err)
