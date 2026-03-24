"""Coordinator tests for AprilAire Cloud."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.api import AprilaireCloudRateLimitError
from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.models import SocketState

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    USERNAME,
    build_hierarchy,
    build_initial_messages,
    build_user,
)


class FakeClient:
    """Minimal fake API client for coordinator tests."""

    def __init__(self) -> None:
        """Initialize the fake client."""
        self.username = USERNAME
        self.session = object()
        self._hierarchy = build_hierarchy()
        self._rate_limit = False

    async def async_get_user(self) -> dict:
        """Return a fake account."""
        return build_user()

    async def async_get_hierarchy(self) -> dict:
        """Return the current fake hierarchy."""
        if self._rate_limit:
            raise AprilaireCloudRateLimitError(120)
        return self._hierarchy

    async def async_get_device_status(self, device_id: str) -> dict:
        """Return status."""
        return build_initial_messages(device_id)[3]

    async def async_get_dehumidifier_status(self, device_id: str) -> dict:
        """Return dehumidifier status."""
        return build_dehumidifier_status(device_id)

    async def async_get_device_settings(self, device_id: str) -> dict:
        """Return device settings."""
        return build_device_settings(device_id)

    async def async_patch_device_settings(self, device_id: str, payload: dict) -> None:
        """Pretend a write succeeded."""
        return None


class FakeWebSocket:
    """Fake websocket manager that injects a bootstrap message batch."""

    def __init__(
        self,
        *,
        client,
        session,
        location_id,
        message_callback,
        state_callback,
    ) -> None:
        """Initialize the websocket."""
        self._location_id = location_id
        self._message_callback = message_callback
        self._state_callback = state_callback

    async def async_start(self) -> None:
        """Publish the initial socket state."""
        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=False)
        )

    async def async_wait_for_initial_sync(self, timeout: float) -> bool:
        """Inject bootstrap data."""
        await self._message_callback(self._location_id, build_initial_messages())
        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
        )
        return True

    async def async_stop(self) -> None:
        """Stop the websocket."""
        return None


async def bootstrap_coordinator(coordinator: AprilaireCloudDataUpdateCoordinator) -> None:
    """Run the coordinator's startup path without config-entry state checks."""
    await coordinator._async_setup()
    coordinator.async_set_updated_data(coordinator._build_snapshot())


@pytest.fixture
def config_entry(hass):
    """Return a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    return entry


async def test_coordinator_bootstrap_marks_supported_devices(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Bootstrap websocket data should mark the device as supported."""
    client = FakeClient()
    monkeypatch.setattr("custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=config_entry, client=client)
    await bootstrap_coordinator(coordinator)

    assert coordinator.data.supported_device_ids == (DEVICE_ID,)
    assert coordinator.data.devices[DEVICE_ID].supported is True
    assert coordinator.data.socket_states[LOCATION_ID].initial_sync_complete is True


async def test_refresh_event_requests_refresh(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """RefreshEvent should schedule a coordinator refresh."""
    client = FakeClient()
    monkeypatch.setattr("custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=config_entry, client=client)
    await bootstrap_coordinator(coordinator)
    coordinator.async_request_refresh = AsyncMock()

    await coordinator.async_process_messages(LOCATION_ID, [{"_type": "RefreshEvent"}])
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_awaited_once()


async def test_rate_limit_maps_to_retry_after(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """REST throttling should become UpdateFailed with retry information."""
    client = FakeClient()
    monkeypatch.setattr("custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=config_entry, client=client)
    await bootstrap_coordinator(coordinator)
    client._rate_limit = True

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert err.value.retry_after == 120
