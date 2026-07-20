"""Runtime data for AprilAire Cloud config entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from homeassistant.loader import Integration

    from .coordinator import AprilaireCloudDataUpdateCoordinator
    from .vendor import AprilaireCloudApiClient


@dataclass(slots=True)
class AprilaireCloudRuntimeData:
    """Runtime objects attached to a config entry."""

    client: AprilaireCloudApiClient
    coordinator: AprilaireCloudDataUpdateCoordinator
    integration: Integration


AprilaireCloudConfigEntry = ConfigEntry[AprilaireCloudRuntimeData]
