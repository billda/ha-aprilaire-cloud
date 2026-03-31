"""Binary sensor platform for AprilAire Cloud."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import AprilaireCloudEntity, setup_dynamic_platform_entities
from .models import SocketState
from .profiles import NormalizedDehumidifierState, get_profile


@dataclass(frozen=True, kw_only=True)
class AprilaireBinarySensorDescription(BinarySensorEntityDescription):
    """Description for an AprilAire binary sensor."""

    value_fn: Callable[[NormalizedDehumidifierState], bool | None]
    enabled_default: bool = True


BINARY_SENSORS: dict[str, AprilaireBinarySensorDescription] = {
    "filter_service": AprilaireBinarySensorDescription(
        key="filter_service",
        translation_key="filter_service",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: normalized.filter_needs_service,
    ),
    "alert_high_humidity": AprilaireBinarySensorDescription(
        key="alert_high_humidity",
        translation_key="alert_high_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: normalized.alert_high_humidity,
    ),
    "alert_low_humidity": AprilaireBinarySensorDescription(
        key="alert_low_humidity",
        translation_key="alert_low_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: normalized.alert_low_humidity,
    ),
    "alert_high_temperature": AprilaireBinarySensorDescription(
        key="alert_high_temperature",
        translation_key="alert_high_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: normalized.alert_high_temperature,
    ),
    "alert_low_temperature": AprilaireBinarySensorDescription(
        key="alert_low_temperature",
        translation_key="alert_low_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: normalized.alert_low_temperature,
    ),
    "compressor": AprilaireBinarySensorDescription(
        key="compressor",
        translation_key="compressor",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: normalized.compressor_on,
        enabled_default=False,
    ),
    "dehumidifier_fan": AprilaireBinarySensorDescription(
        key="dehumidifier_fan",
        translation_key="dehumidifier_fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: normalized.dehumidifier_fan_on,
        enabled_default=False,
    ),
    "hvac_fan": AprilaireBinarySensorDescription(
        key="hvac_fan",
        translation_key="hvac_fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: normalized.hvac_fan_on,
        enabled_default=False,
    ),
}


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire binary sensors."""
    coordinator = entry.runtime_data.coordinator

    def _entities_for_device(device_id: str, device):
        profile = get_profile(coordinator.data.devices[device_id].profile_key)
        if profile is None:
            return
        entity_set = profile.entity_descriptions(coordinator.data.devices[device_id])
        for key in entity_set.binary_sensor_keys:
            description = BINARY_SENSORS.get(key)
            if description is not None:
                yield AprilaireBinarySensorEntity(coordinator, device_id, description)

    setup_dynamic_platform_entities(entry, async_add_entities, _entities_for_device)

    active_ws_entities: dict[str, AprilaireWebSocketStatusEntity] = {}

    def _sync_ws_entities() -> None:
        wanted = set(coordinator.data.locations)
        current = set(active_ws_entities)

        removed = current - wanted
        for location_id in removed:
            entity = active_ws_entities.pop(location_id)
            if entity.hass is not None:
                coordinator.hass.async_create_task(entity.async_remove(force_remove=False))

        new_entities = []
        for location_id in wanted - current:
            entity = AprilaireWebSocketStatusEntity(coordinator, location_id)
            active_ws_entities[location_id] = entity
            new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

    _sync_ws_entities()
    entry.async_on_unload(coordinator.async_add_listener(_sync_ws_entities))


class AprilaireBinarySensorEntity(AprilaireCloudEntity, BinarySensorEntity):
    """An AprilAire binary sensor."""

    entity_description: AprilaireBinarySensorDescription

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
        description: AprilaireBinarySensorDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_entity_category = description.entity_category
        self._attr_entity_registry_enabled_default = description.enabled_default or (
            description.entity_category is EntityCategory.DIAGNOSTIC
            and coordinator.extra_diagnostics_enabled
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the binary sensor is on."""
        normalized = self.normalized_device
        if normalized is None:
            return None
        return self.entity_description.value_fn(normalized)


class AprilaireWebSocketStatusEntity(
    CoordinatorEntity[AprilaireCloudDataUpdateCoordinator], BinarySensorEntity
):
    """Binary sensor for WebSocket connection status per location."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "websocket_connection"

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        location_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._location_id = location_id
        self._attr_unique_id = f"{location_id}_websocket_connection"
        self._attr_entity_registry_enabled_default = coordinator.extra_diagnostics_enabled

    @property
    def _socket_state(self) -> SocketState | None:
        """Return the current socket state for this location."""
        return self.coordinator.data.socket_states.get(self._location_id)

    @property
    def is_on(self) -> bool | None:
        """Return whether the WebSocket is connected and synced."""
        state = self._socket_state
        if state is None:
            return None
        return state.connected and state.initial_sync_complete

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self._socket_state is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose reconnect and error details."""
        state = self._socket_state
        if state is None:
            return {}
        return {
            "reconnect_attempt": state.reconnect_attempt,
            "last_error": state.last_error,
        }

    @property
    def device_info(self):
        """Return device info linking to the first device in this location."""
        location = self.coordinator.data.locations.get(self._location_id)
        name = location.name if location else self._location_id
        return {
            "identifiers": {(DOMAIN, f"location_{self._location_id}")},
            "manufacturer": MANUFACTURER,
            "name": f"{name} Cloud Connection",
        }
