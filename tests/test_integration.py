"""Integration setup tests for AprilAire Cloud."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant import config_entries
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.aprilaire_cloud as integration
from custom_components.aprilaire_cloud.const import (
    CONF_ENABLE_EXTRA_DIAGNOSTICS,
    DOMAIN,
)
from custom_components.aprilaire_cloud.models import SocketState

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
    ThermostatFakeWebSocket,
    build_hierarchy,
    build_initial_messages,
    build_thermostat_hierarchy,
    build_thermostat_initial_messages,
    build_thermostat_settings,
    build_two_location_hierarchy,
    build_user,
)


async def test_identity_mismatch_shuts_down_started_websockets(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Identity validation failure cannot leak background transports."""
    client = FakeClient()
    FakeWebSocket.instances.clear()
    monkeypatch.setattr(
        integration, "AprilaireCloudApiClient", lambda username, password, session: client
    )
    monkeypatch.setattr(
        integration, "async_get_loaded_integration", lambda hass, domain: SimpleNamespace()
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id="different-user-001",
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, config_entries.ConfigEntryState.SETUP_IN_PROGRESS)

    with pytest.raises(ConfigEntryAuthFailed):
        await integration.async_setup_entry(hass, entry)

    assert FakeWebSocket.instances[LOCATION_ID].stopped is True


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


async def test_remove_device_guard_only_blocks_live_physical_devices(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Only physical devices still reported by the account should be protected."""
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
            SimpleNamespace(identifiers={(DOMAIN, DEVICE_ID)}),
        )
        is False
    )
    assert (
        await integration.async_remove_config_entry_device(
            hass,
            entry,
            SimpleNamespace(identifiers={(DOMAIN, f"location_{LOCATION_ID}")}),
        )
        is True
    )


async def test_setup_removes_legacy_location_connection_device(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Upgrades should preserve the diagnostic entity while removing its old device."""
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
        options={CONF_ENABLE_EXTRA_DIAGNOSTICS: True},
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    legacy_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"location_{LOCATION_ID}")},
        manufacturer="AprilAire",
        name="Home Cloud Connection",
    )
    legacy_entity = entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{LOCATION_ID}_websocket_connection",
        config_entry=entry,
        device_id=legacy_device.id,
        suggested_object_id="home_cloud_connection",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    migrated_entity = entity_registry.async_get(legacy_entity.entity_id)
    assert migrated_entity is not None
    assert migrated_entity.unique_id == legacy_entity.unique_id
    assert migrated_entity.device_id is None
    assert hass.states.get(legacy_entity.entity_id) is not None
    assert device_registry.async_get(legacy_device.id) is None
    assert any(
        identifier == (DOMAIN, DEVICE_ID)
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for identifier in device.identifiers
    )


async def test_setup_creates_climate_entities_for_thermostat_accounts(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Thermostat-only accounts should create stable zone climate entities."""
    client = FakeClient()
    client._hierarchy = build_thermostat_hierarchy()
    client.device_settings = build_thermostat_settings()
    FakeWebSocket.instances.clear()

    monkeypatch.setattr(
        integration, "AprilaireCloudApiClient", lambda username, password, session: client
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        ThermostatFakeWebSocket,
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
    unique_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }

    assert {
        "device-thermostat-001_thermostat_pz1",
        "device-thermostat-001_thermostat_sz2",
        "device-thermostat-001_thermostat_sz3",
    }.issubset(unique_ids)


async def test_mixed_dehumidifier_and_thermostat_accounts_create_both_families(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Mixed accounts should surface both existing and beta device families."""

    class MixedFakeWebSocket(FakeWebSocket):
        async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
            await self._message_callback(
                self._location_id,
                [
                    *build_initial_messages(DEVICE_ID),
                    *build_thermostat_initial_messages(),
                ],
            )
            await self._state_callback(
                SocketState(
                    location_id=self._location_id,
                    connected=True,
                    initial_sync_complete=True,
                )
            )
            return True

    client = FakeClient()
    client._hierarchy = {
        "locations": [
            {
                "locationId": LOCATION_ID,
                "name": "Home",
                "timeZone": "America/New_York",
                "rooms": [
                    {
                        "name": "Basement",
                        "devices": [{"deviceId": DEVICE_ID, "access": "manage", "zone": 1}],
                    },
                    {
                        "name": "Zone One",
                        "devices": [
                            {"deviceId": "device-thermostat-001", "access": "manage", "zone": 1}
                        ],
                    },
                ],
            }
        ]
    }
    FakeWebSocket.instances.clear()

    monkeypatch.setattr(
        integration, "AprilaireCloudApiClient", lambda username, password, session: client
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        MixedFakeWebSocket,
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
    unique_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }

    assert f"{DEVICE_ID}_dehumidifier" in unique_ids
    assert "device-thermostat-001_thermostat_pz1" in unique_ids


async def test_removed_locations_cleanup_and_recreate_websocket_entities(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Location websocket entities should cycle without creating synthetic devices."""
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
    assert second_ws_entity.device_id is None
    assert not any(
        identifier[0] == DOMAIN and identifier[1].startswith("location_")
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
    assert second_ws_entity.device_id is None
    assert not any(
        identifier[0] == DOMAIN and identifier[1].startswith("location_")
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for identifier in device.identifiers
    )
