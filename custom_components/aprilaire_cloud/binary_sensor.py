"""Binary sensor platform for AprilAire Cloud."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .entity import (
    AprilaireCloudEntity,
    DynamicEntityDescriptor,
    setup_dynamic_platform_entities,
)
from .models import SocketState
from .profiles import NormalizedDehumidifierState, NormalizedThermostatState, get_profile

_WEBSOCKET_CONNECTION_SUFFIX = "_websocket_connection"


@dataclass(frozen=True, kw_only=True)
class AprilaireBinarySensorDescription(BinarySensorEntityDescription):
    """Description for an AprilAire binary sensor."""

    value_fn: Callable[[object], bool | None]
    enabled_default: bool = True


def _dehumidifier_state(normalized: object) -> NormalizedDehumidifierState:
    """Return dehumidifier-normalized state."""
    return cast(NormalizedDehumidifierState, normalized)


DEHUMIDIFIER_BINARY_SENSORS: dict[str, AprilaireBinarySensorDescription] = {
    "filter_service": AprilaireBinarySensorDescription(
        key="filter_service",
        translation_key="filter_service",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: _dehumidifier_state(normalized).filter_needs_service,
    ),
    "alert_high_humidity": AprilaireBinarySensorDescription(
        key="alert_high_humidity",
        translation_key="alert_high_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: _dehumidifier_state(normalized).alert_high_humidity,
    ),
    "alert_low_humidity": AprilaireBinarySensorDescription(
        key="alert_low_humidity",
        translation_key="alert_low_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: _dehumidifier_state(normalized).alert_low_humidity,
    ),
    "alert_high_temperature": AprilaireBinarySensorDescription(
        key="alert_high_temperature",
        translation_key="alert_high_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: _dehumidifier_state(normalized).alert_high_temperature,
    ),
    "alert_low_temperature": AprilaireBinarySensorDescription(
        key="alert_low_temperature",
        translation_key="alert_low_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda normalized: _dehumidifier_state(normalized).alert_low_temperature,
    ),
    "compressor": AprilaireBinarySensorDescription(
        key="compressor",
        translation_key="compressor",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: _dehumidifier_state(normalized).compressor_on,
        enabled_default=False,
    ),
    "dehumidifier_fan": AprilaireBinarySensorDescription(
        key="dehumidifier_fan",
        translation_key="dehumidifier_fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: _dehumidifier_state(normalized).dehumidifier_fan_on,
        enabled_default=False,
    ),
    "hvac_fan": AprilaireBinarySensorDescription(
        key="hvac_fan",
        translation_key="hvac_fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda normalized: _dehumidifier_state(normalized).hvac_fan_on,
        enabled_default=False,
    ),
}
PROFILE_BINARY_SENSOR_DESCRIPTIONS: dict[
    str, dict[str, AprilaireBinarySensorDescription]
] = {
    "dehumidifier": DEHUMIDIFIER_BINARY_SENSORS,
}


def _descriptors_for_device(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    device_id: str,
    device: Any,
):
    """Build stable descriptors for one device's binary sensors."""
    profile = get_profile(device.profile_key)
    if profile is None:
        return
    entity_set = profile.entity_descriptions(device)
    descriptions = PROFILE_BINARY_SENSOR_DESCRIPTIONS.get(device.profile_key, {})
    for key in entity_set.binary_sensor_keys:
        description = descriptions.get(key)
        if description is not None:
            yield DynamicEntityDescriptor(
                unique_id=f"{device_id}_{description.key}",
                factory=AprilaireBinarySensorEntity,
                args=(coordinator, device_id, description),
            )
        elif key == "attached_humidifier_water_panel_service":
            yield DynamicEntityDescriptor(
                unique_id=f"{device_id}_attached_humidifier_water_panel_service",
                factory=AprilaireAttachedHumidifierServiceEntity,
                args=(coordinator, device_id),
            )


def _setup_websocket_status_entities(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    entry: AprilaireCloudConfigEntry,
    async_add_entities,
) -> None:
    """Synchronize location-scoped WebSocket diagnostic entities."""
    active: dict[str, AprilaireWebSocketStatusEntity] = {}

    def _sync() -> None:
        wanted = set(coordinator.data.locations)
        current = set(active)

        for location_id in current - wanted:
            entity = active.pop(location_id)
            if entity.hass is not None:
                coordinator.hass.async_create_task(entity.async_remove(force_remove=False))

        new_entities = []
        for location_id in wanted - current:
            entity = AprilaireWebSocketStatusEntity(coordinator, location_id)
            active[location_id] = entity
            new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


def _remove_legacy_websocket_devices(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    entry: AprilaireCloudConfigEntry,
) -> None:
    """Remove obsolete synthetic devices without deleting their diagnostic entities."""
    device_registry = dr.async_get(coordinator.hass)
    entity_registry = er.async_get(coordinator.hass)
    physical_identifiers = {
        (DOMAIN, device_id) for device_id in coordinator.data.devices
    }
    location_identifiers = {
        (DOMAIN, f"location_{location_id}")
        for location_id in coordinator.data.locations
    }
    legacy_device_ids = {
        device.id
        for device in dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        )
        if device.identifiers.isdisjoint(physical_identifiers)
        and not device.identifiers.isdisjoint(location_identifiers)
    }

    for registry_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if (
            registry_entry.platform != DOMAIN
            or registry_entry.device_id is None
            or not registry_entry.unique_id.endswith(_WEBSOCKET_CONNECTION_SUFFIX)
        ):
            continue
        location_id = registry_entry.unique_id.removesuffix(
            _WEBSOCKET_CONNECTION_SUFFIX
        )
        device = device_registry.async_get(registry_entry.device_id)
        if (
            device is not None
            and (DOMAIN, f"location_{location_id}") in device.identifiers
            and device.identifiers.isdisjoint(physical_identifiers)
        ):
            legacy_device_ids.add(device.id)

    for device_id in legacy_device_ids:
        for registry_entry in er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=True
        ):
            if registry_entry.disabled_by is RegistryEntryDisabler.DEVICE:
                entity_registry.async_update_entity(
                    registry_entry.entity_id,
                    device_id=None,
                    disabled_by=RegistryEntryDisabler.USER,
                )
            else:
                entity_registry.async_update_entity(
                    registry_entry.entity_id,
                    device_id=None,
                )
        if device_registry.async_get(device_id) is not None:
            device_registry.async_remove_device(device_id)


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up AprilAire binary sensors."""
    coordinator = entry.runtime_data.coordinator
    _remove_legacy_websocket_devices(coordinator, entry)

    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        lambda device_id, device: _descriptors_for_device(
            coordinator, device_id, device
        ),
    )
    _setup_websocket_status_entities(coordinator, entry, async_add_entities)


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
        normalized = self.normalized_state
        if normalized is None:
            return None
        return self.entity_description.value_fn(normalized)


class AprilaireAttachedHumidifierServiceEntity(
    AprilaireCloudEntity,
    BinarySensorEntity,
):
    """Reported water-panel service alert for an attached humidifier."""

    _attr_translation_key = "attached_humidifier_water_panel_service"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        """Initialize the water-panel service entity."""
        super().__init__(
            coordinator,
            device_id,
            "attached_humidifier_water_panel_service",
        )

    @property
    def is_on(self) -> bool | None:
        """Return the reported service flag."""
        normalized = cast(NormalizedThermostatState | None, self.normalized_state)
        if normalized is None or normalized.attached_humidifier is None:
            return None
        return normalized.attached_humidifier.water_panel_needs_service


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
        self._attr_unique_id = f"{location_id}{_WEBSOCKET_CONNECTION_SUFFIX}"
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
