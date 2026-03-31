"""Integration setup tests for AprilAire Cloud."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.aprilaire_cloud as integration
from custom_components.aprilaire_cloud.const import (
    CONF_ENABLE_EXTRA_DIAGNOSTICS,
    DOMAIN,
)

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    SECOND_DEVICE_ID,
    SECOND_LOCATION_ID,
    USERNAME,
    FakeClient,
    FakeWebSocket,
    MultiLocationFakeWebSocket,
    build_hierarchy,
    build_initial_messages,
    build_two_location_hierarchy,
    build_user,
)


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

    client._hierarchy = build_hierarchy(include_second_device=True)
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

    client._hierarchy = build_hierarchy(include_second_device=True)
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

    client._hierarchy = build_hierarchy()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert SECOND_DEVICE_ID not in entry.runtime_data.coordinator.data.devices
    assert all(hass.states.get(entity_id) is None for entity_id in second_entity_ids)

    client._hierarchy = build_hierarchy(include_second_device=True)
    await entry.runtime_data.coordinator.async_refresh()
    await FakeWebSocket.instances[LOCATION_ID].push_messages(
        build_initial_messages(SECOND_DEVICE_ID)
    )
    await hass.async_block_till_done()

    assert any(
        entity.unique_id and entity.unique_id.startswith(f"{SECOND_DEVICE_ID}_")
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    )


async def test_remove_device_guard_blocks_live_location_devices(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Synthetic location devices should be protected while the location is still active."""
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

    assert (
        await integration.async_remove_config_entry_device(
            hass,
            entry,
            SimpleNamespace(identifiers={(DOMAIN, f"location_{LOCATION_ID}")}),
        )
        is False
    )
    assert (
        await integration.async_remove_config_entry_device(
            hass,
            entry,
            SimpleNamespace(identifiers={(DOMAIN, "location_stale-location")}),
        )
        is True
    )


async def test_removed_locations_cleanup_and_recreate_websocket_entities(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Location websocket entities and devices should cleanly remove and re-add."""
    client = FakeClient()
    client._hierarchy = build_two_location_hierarchy()
    FakeWebSocket.instances.clear()

    monkeypatch.setattr(
        integration, "AprilaireCloudApiClient", lambda username, password, session: client
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        MultiLocationFakeWebSocket,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
        options={CONF_ENABLE_EXTRA_DIAGNOSTICS: True},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    second_ws_unique_id = f"{SECOND_LOCATION_ID}_websocket_connection"
    second_ws_entity = next(
        entity
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if entity.unique_id == second_ws_unique_id
    )

    assert hass.states.get(second_ws_entity.entity_id) is not None
    assert any(
        identifier == (DOMAIN, f"location_{SECOND_LOCATION_ID}")
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for identifier in device.identifiers
    )

    client._hierarchy = build_hierarchy()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert SECOND_LOCATION_ID not in entry.runtime_data.coordinator.data.locations
    assert not any(
        entity.unique_id == second_ws_unique_id
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    )
    assert not any(
        identifier == (DOMAIN, f"location_{SECOND_LOCATION_ID}")
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for identifier in device.identifiers
    )

    client._hierarchy = build_two_location_hierarchy()
    await entry.runtime_data.coordinator.async_refresh()
    await FakeWebSocket.instances[SECOND_LOCATION_ID].push_messages(
        build_initial_messages(SECOND_DEVICE_ID)
    )
    await hass.async_block_till_done()

    second_ws_entity = next(
        entity
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if entity.unique_id == second_ws_unique_id
    )
    assert hass.states.get(second_ws_entity.entity_id) is not None
    assert any(
        identifier == (DOMAIN, f"location_{SECOND_LOCATION_ID}")
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for identifier in device.identifiers
    )
