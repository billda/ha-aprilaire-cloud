"""Integration setup tests for AprilAire Cloud."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.aprilaire_cloud as integration
from custom_components.aprilaire_cloud.const import DOMAIN

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    SECOND_DEVICE_ID,
    USERNAME,
    build_hierarchy,
    build_initial_messages,
    build_user,
)


class FakeClient:
    """Mutable fake API client used for end-to-end setup tests."""

    def __init__(self) -> None:
        """Initialize the fake client."""
        self.username = USERNAME
        self.session = object()
        self.hierarchy = build_hierarchy()

    async def async_authenticate(self) -> None:
        """No-op auth."""
        return None

    async def async_get_user(self) -> dict:
        """Return the fake account."""
        return build_user()

    async def async_get_hierarchy(self) -> dict:
        """Return the current hierarchy."""
        return self.hierarchy

    async def async_get_device_status(self, device_id: str) -> dict:
        """Return bootstrap device status."""
        return build_initial_messages(device_id)[3]

    async def async_get_dehumidifier_status(self, device_id: str) -> dict:
        """Return bootstrap dehumidifier status."""
        return build_initial_messages(device_id)[0]

    async def async_get_device_settings(self, device_id: str) -> dict:
        """Return bootstrap settings."""
        return build_initial_messages(device_id)[1]

    async def async_patch_device_settings(self, device_id: str, payload: dict) -> None:
        """Pretend writes succeed."""
        return None


class FakeWebSocket:
    """Fake websocket that only pushes bootstrap data when asked."""

    instances: ClassVar[dict[str, FakeWebSocket]] = {}

    def __init__(
        self,
        *,
        client,
        session,
        location_id,
        message_callback,
        state_callback,
    ) -> None:
        """Initialize the fake websocket."""
        self._location_id = location_id
        self._message_callback = message_callback
        self._state_callback = state_callback
        FakeWebSocket.instances[location_id] = self

    async def async_start(self) -> None:
        """Connect immediately."""
        from custom_components.aprilaire_cloud.models import SocketState

        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=False)
        )

    async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
        """Push the initial state."""
        await self.push_messages(build_initial_messages())
        return True

    async def async_stop(self) -> None:
        """Stop the websocket."""
        return None

    async def push_messages(self, messages: list[dict]) -> None:
        """Push custom websocket messages into the coordinator."""
        from custom_components.aprilaire_cloud.models import SocketState

        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
        )
        await self._message_callback(self._location_id, messages)


async def test_setup_creates_entities_and_new_devices_surface_automatically(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Supported devices should be created at setup and after later discovery."""
    client = FakeClient()
    FakeWebSocket.instances.clear()

    monkeypatch.setattr(
        integration, "AprilaireCloudApiClient", lambda username, password, session: client
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    assert any(
        identifier == (DOMAIN, DEVICE_ID)
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for identifier in device.identifiers
    )

    client.hierarchy = build_hierarchy(include_second_device=True)
    await entry.runtime_data.coordinator.async_request_refresh()
    await FakeWebSocket.instances[LOCATION_ID].push_messages(
        build_initial_messages(SECOND_DEVICE_ID)
    )
    await hass.async_block_till_done()

    assert any(
        entity.unique_id and entity.unique_id.startswith(f"{SECOND_DEVICE_ID}_")
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    )


async def test_removed_devices_can_be_readded_without_reloading(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Dynamic discovery should handle remove and re-add cycles cleanly."""
    client = FakeClient()
    FakeWebSocket.instances.clear()

    monkeypatch.setattr(
        integration, "AprilaireCloudApiClient", lambda username, password, session: client
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)

    client.hierarchy = build_hierarchy(include_second_device=True)
    await entry.runtime_data.coordinator.async_refresh()
    await FakeWebSocket.instances[LOCATION_ID].push_messages(
        build_initial_messages(SECOND_DEVICE_ID)
    )
    await hass.async_block_till_done()

    second_entity_ids = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if entity.unique_id and entity.unique_id.startswith(f"{SECOND_DEVICE_ID}_")
    ]
    assert second_entity_ids
    assert any(
        entity.unique_id and entity.unique_id.startswith(f"{SECOND_DEVICE_ID}_")
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    )

    client.hierarchy = build_hierarchy()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert SECOND_DEVICE_ID not in entry.runtime_data.coordinator.data.devices
    assert all(hass.states.get(entity_id) is None for entity_id in second_entity_ids)

    client.hierarchy = build_hierarchy(include_second_device=True)
    await entry.runtime_data.coordinator.async_refresh()
    await FakeWebSocket.instances[LOCATION_ID].push_messages(
        build_initial_messages(SECOND_DEVICE_ID)
    )
    await hass.async_block_till_done()

    assert any(
        entity.unique_id and entity.unique_id.startswith(f"{SECOND_DEVICE_ID}_")
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    )
