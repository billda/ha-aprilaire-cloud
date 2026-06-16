"""Shared entity helpers for AprilAire Cloud."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AprilaireCloudApiError, AprilaireCloudRateLimitError
from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import AprilaireCloudDataUpdateCoordinator
from .data import AprilaireCloudConfigEntry
from .models import DeviceRecord
from .profiles import DeviceProfile, get_profile, normalize_device


def raise_ha_write_error(err: Exception) -> None:
    """Raise a translated Home Assistant error for a typed integration failure."""
    if isinstance(err, AprilaireCloudRateLimitError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={"seconds": str(round(err.retry_after or 0))},
        ) from err

    if isinstance(err, AprilaireCloudApiError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="write_failed",
        ) from err

    raise err


def setup_dynamic_platform_entities(
    entry: AprilaireCloudConfigEntry,
    async_add_entities: Callable[[Iterable[Any]], None],
    entity_factory: Callable[[str, DeviceRecord], Iterable[Any]],
) -> None:
    """Set up dynamic entities for a platform with add/remove support."""
    coordinator = entry.runtime_data.coordinator
    active_entities: dict[str, Any] = {}

    def _sync_entities() -> None:
        desired_entities: dict[str, Any] = {}
        for device_id, device in coordinator.data.devices.items():
            if not device.supported:
                continue
            for entity in entity_factory(device_id, device):
                desired_entities[entity.unique_id] = entity

        removed_unique_ids = set(active_entities) - set(desired_entities)
        for unique_id in removed_unique_ids:
            entity = active_entities.pop(unique_id)
            if entity.hass is not None:
                coordinator.hass.async_create_task(entity.async_remove(force_remove=False))

        new_entities = [
            entity
            for unique_id, entity in desired_entities.items()
            if unique_id not in active_entities
        ]
        if new_entities:
            for entity in new_entities:
                active_entities[entity.unique_id] = entity
            async_add_entities(new_entities)

    _sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(_sync_entities))


class AprilaireCloudEntity(CoordinatorEntity[AprilaireCloudDataUpdateCoordinator]):
    """Base entity for AprilAire Cloud."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AprilaireCloudDataUpdateCoordinator, device_id: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}"

    @property
    def device(self) -> DeviceRecord | None:
        """Return the current device record."""
        return self.coordinator.data.devices.get(self._device_id)

    @property
    def effective_device_settings(self) -> dict[str, Any]:
        """Return the effective writable settings for the current device."""
        device = self.device
        if device is None:
            return {}
        return device.effective_device_settings

    @property
    def available(self) -> bool:
        """Return whether the entity is online and verified fresh by the data coordinator."""
        device = self.device
        if device is None or not device.supported:
            return False

        # Read our custom hardware offline flag injected during the snapshot build step
        status = getattr(device, "device_status", {})
        if isinstance(status, dict) and status.get("_hardware_offline") is True:
            return False

        return super().available

    @property
    def profile(self) -> DeviceProfile | None:
        """Return the resolved profile for the current device."""
        device = self.device
        if device is None:
            return None
        return get_profile(device.profile_key)

    @property
    def normalized_state(self) -> object | None:
        """Return normalized profile state for supported devices."""
        device = self.device
        if device is None:
            return None
        return normalize_device(device)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry metadata."""
        device = self.device
        assert device is not None
        status = device.device_status
        name_parts = [device.hierarchy.location_name]
        if device.hierarchy.room_name:
            name_parts.append(device.hierarchy.room_name)
        if model := status.get("model"):
            name_parts.append(model)

        info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer=MANUFACTURER,
            name=" ".join(name_parts),
            suggested_area=device.hierarchy.room_name,
            model=status.get("model"),
            sw_version=status.get("firmwareRev"),
        )
        if hardware := status.get("hardwareRev"):
            info["hw_version"] = str(hardware)
        return info

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose shared attributes."""
        return {}
