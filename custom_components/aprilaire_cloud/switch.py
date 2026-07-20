"""Switch platform for on/off-only AprilAire capabilities."""

from __future__ import annotations

from typing import cast

from homeassistant.components.switch import SwitchEntity

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
    SetDehumidifierPower,
    get_profile,
)
from .vendor import AprilaireCloudApiError, AprilaireCloudRateLimitError


async def async_setup_entry(hass, entry: AprilaireCloudConfigEntry, async_add_entities) -> None:
    """Set up honest on/off-only control entities."""
    coordinator = entry.runtime_data.coordinator

    def _descriptors_for_device(device_id: str, device):
        profile = get_profile(device.profile_key)
        if profile is None:
            return
        if "dehumidifier_power" in profile.entity_descriptions(device).switch_keys:
            yield DynamicEntityDescriptor(
                unique_id=f"{device_id}_dehumidifier_power",
                factory=AprilaireDehumidifierPowerSwitch,
                args=(coordinator, device_id),
            )

    setup_dynamic_platform_entities(
        entry,
        async_add_entities,
        _descriptors_for_device,
    )


class AprilaireDehumidifierPowerSwitch(AprilaireCloudEntity, SwitchEntity):
    """On/off control for a dehumidifier without a usable humidity target."""

    _attr_translation_key = "dehumidifier_power"

    def __init__(
        self,
        coordinator: AprilaireCloudDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        """Initialize the on/off-only entity."""
        super().__init__(coordinator, device_id, "dehumidifier_power")

    @property
    def is_on(self) -> bool | None:
        """Return the normalized power state."""
        normalized = cast(NormalizedDehumidifierState | None, self.normalized_state)
        if normalized is None or normalized.mode not in {"on", "off"}:
            return None
        return normalized.mode == "on"

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the dehumidifier on."""
        await self._async_set_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the dehumidifier off."""
        await self._async_set_power(False)

    async def _async_set_power(self, enabled: bool) -> None:
        """Execute the profile-owned power command."""
        try:
            await self.coordinator.async_execute_command(
                self._device_id,
                SetDehumidifierPower(enabled=enabled),
            )
        except (
            AprilaireCommandError,
            AprilaireCloudRateLimitError,
            AprilaireCloudApiError,
        ) as err:
            raise_ha_write_error(err)
