"""Coordinator tests for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.api import (
    AprilaireCloudCommunicationError,
    AprilaireCloudWriteError,
)
from custom_components.aprilaire_cloud.const import (
    DOMAIN,
    ISSUE_NO_SUPPORTED_DEVICES,
)
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
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
    bootstrap_coordinator,
    build_dehumidifier_status,
    build_device_settings,
    build_device_setup,
    build_device_status,
    build_hierarchy,
    build_initial_messages,
    build_two_location_hierarchy,
    build_user,
    wait_until,
)


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


async def _async_set_target_humidity(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    humidity: int,
) -> None:
    """Write the dehumidifier humidity setpoint."""
    await coordinator.async_write_device_settings(
        DEVICE_ID,
        {"dehumidifier": {"humiditySetpoint": humidity}},
    )


async def _async_set_alert_limit(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    key: str,
    value: int,
) -> None:
    """Write a dehumidifier alert limit."""
    await coordinator.async_write_device_settings(
        DEVICE_ID,
        {"dehumidifier": {"alertLimits": {key: value}}},
    )


async def test_coordinator_bootstrap_marks_supported_devices(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Bootstrap websocket data should mark the device as supported."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
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
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
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
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)
    client._rate_limit = True

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert err.value.retry_after == 120


async def test_single_humidity_write_is_optimistic_and_requires_device_settings_confirmation(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Target humidity should update immediately but only confirm on DeviceSettings."""
    client = FakeClient()
    client.patch_release = asyncio.Event()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    task = asyncio.create_task(_async_set_target_humidity(coordinator, 55))
    await asyncio.wait_for(client.patch_started.wait(), timeout=1)

    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 55
    )
    assert (
        coordinator.data.devices[DEVICE_ID].device_settings["dehumidifier"]["humiditySetpoint"]
        == 52
    )

    client.patch_release.set()
    await hass.async_block_till_done()

    await coordinator.async_process_messages(LOCATION_ID, [build_dehumidifier_status()])
    await asyncio.sleep(0)
    assert not task.done()

    await coordinator.async_process_messages(LOCATION_ID, [build_device_settings(humidity=55)])
    await asyncio.wait_for(task, timeout=1)

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}
    assert (
        coordinator.data.devices[DEVICE_ID].device_settings["dehumidifier"]["humiditySetpoint"]
        == 55
    )


async def test_successful_write_clears_internal_write_state(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Successful writes should not leave stale write-state bookkeeping behind."""
    client = FakeClient()
    client.patch_release = asyncio.Event()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    task = asyncio.create_task(_async_set_target_humidity(coordinator, 55))
    await asyncio.wait_for(client.patch_started.wait(), timeout=1)

    client.patch_release.set()
    await hass.async_block_till_done()
    await coordinator.async_process_messages(LOCATION_ID, [build_device_settings(humidity=55)])
    await asyncio.wait_for(task, timeout=1)
    await asyncio.sleep(0)

    assert DEVICE_ID not in coordinator._write_states


async def test_rapid_humidity_writes_are_last_write_wins(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A stale confirmation must not overwrite a newer optimistic value."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    first = asyncio.create_task(_async_set_target_humidity(coordinator, 45))
    await wait_until(lambda: len(client.patched_payloads) == 1)

    second = asyncio.create_task(_async_set_target_humidity(coordinator, 50))
    await asyncio.sleep(0)

    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 50
    )

    await coordinator.async_process_messages(LOCATION_ID, [build_device_settings(humidity=45)])
    await asyncio.wait_for(first, timeout=1)

    assert (
        coordinator.data.devices[DEVICE_ID].device_settings["dehumidifier"]["humiditySetpoint"]
        == 45
    )
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 50
    )

    await wait_until(lambda: len(client.patched_payloads) == 2)
    assert client.patched_payloads[0]["dehumidifier"]["humiditySetpoint"] == 45
    assert client.patched_payloads[1]["dehumidifier"]["humiditySetpoint"] == 50

    await coordinator.async_process_messages(LOCATION_ID, [build_device_settings(humidity=50)])
    await asyncio.wait_for(second, timeout=1)

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 50
    )


async def test_failed_older_write_reverts_only_its_own_paths(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A failed older write should not clear a newer unrelated optimistic change."""
    client = FakeClient()
    client.patch_release = asyncio.Event()
    client.patch_side_effects = [AprilaireCloudCommunicationError("write failed"), None]
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    first = asyncio.create_task(_async_set_target_humidity(coordinator, 55))
    await asyncio.wait_for(client.patch_started.wait(), timeout=1)

    second = asyncio.create_task(_async_set_alert_limit(coordinator, "highHum", 70))
    await asyncio.sleep(0)

    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 55
    )
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "alertLimits"
        ]["highHum"]
        == 70
    )

    client.patch_release.set()
    with pytest.raises(AprilaireCloudCommunicationError):
        await first

    settings = build_device_settings(humidity=52)
    settings["dehumidifier"]["alertLimits"]["highHum"] = 70
    await coordinator.async_process_messages(LOCATION_ID, [settings])
    await asyncio.wait_for(second, timeout=1)

    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 52
    )
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "alertLimits"
        ]["highHum"]
        == 70
    )


async def test_partial_device_settings_confirmation_clears_only_matching_pending_paths(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Partial DeviceSettings payloads should preserve unrelated confirmed settings."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    device = coordinator.data.devices[DEVICE_ID]
    coordinator._devices[DEVICE_ID] = replace(
        device,
        pending_device_settings={
            "dehumidifier": {"humiditySetpoint": 60, "alertLimits": {"highHum": 70}}
        },
    )
    coordinator._sync_write_state(DEVICE_ID, confirmed_settings=None)

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "DeviceSettings",
                "deviceId": DEVICE_ID,
                "dehumidifier": {"humiditySetpoint": 60},
            }
        ],
    )

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {
        "dehumidifier": {"alertLimits": {"highHum": 70}}
    }
    assert (
        coordinator.data.devices[DEVICE_ID].device_settings["dehumidifier"]["alertLimits"][
            "highHum"
        ]
        == 65
    )


async def test_fallback_refresh_only_targets_devices_in_unhealthy_locations(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """REST fallback should only refresh devices whose location socket is unhealthy."""
    client = FakeClient()
    client._hierarchy = build_two_location_hierarchy()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        MultiLocationFakeWebSocket,
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    client.requested_status_ids.clear()
    client.requested_status_endpoints.clear()
    client.requested_settings_ids.clear()

    await coordinator.async_socket_state_changed(
        SocketState(
            location_id=SECOND_LOCATION_ID,
            connected=False,
            initial_sync_complete=False,
        )
    )

    await coordinator._async_update_data()

    assert client.requested_status_ids == [SECOND_DEVICE_ID]
    assert client.requested_status_endpoints == [(SECOND_DEVICE_ID, "dehumidifier")]
    assert client.requested_settings_ids == [SECOND_DEVICE_ID]


async def test_terminally_rejected_devices_do_not_drive_rest_fallback(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A terminally unsupported device should not force REST fallback refreshes."""

    class TerminalUnsupportedFakeWebSocket(FakeWebSocket):
        async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
            await self._message_callback(
                self._location_id,
                [
                    {
                        "_type": "DeviceSetup",
                        "deviceId": DEVICE_ID,
                        "type": "thermostat",
                    },
                    build_device_status(DEVICE_ID),
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
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        TerminalUnsupportedFakeWebSocket,
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    assert coordinator.data.devices[DEVICE_ID].supported is False
    assert coordinator.data.devices[DEVICE_ID].unsupported_reason == "unsupported_equipment_type"
    assert client.requested_status_ids == []
    assert client.requested_status_endpoints == []
    assert client.requested_settings_ids == []

    await coordinator.async_socket_state_changed(
        SocketState(
            location_id=LOCATION_ID,
            connected=False,
            initial_sync_complete=False,
        )
    )
    await coordinator._async_update_data()

    assert client.requested_status_ids == []
    assert client.requested_status_endpoints == []
    assert client.requested_settings_ids == []


async def test_partial_rest_refresh_failure_keeps_successful_devices_updated(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A failed device refresh should not prevent sibling devices from updating."""
    client = FakeClient()
    client._hierarchy = build_hierarchy(include_second_device=True)
    client.rest_failures[("device_status", SECOND_DEVICE_ID)] = AprilaireCloudCommunicationError(
        "status unavailable"
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        MultiLocationFakeWebSocket,
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    await coordinator.async_socket_state_changed(
        SocketState(
            location_id=LOCATION_ID,
            connected=False,
            initial_sync_complete=False,
        )
    )

    await coordinator._async_update_data()

    assert DEVICE_ID in client.requested_status_ids
    assert SECOND_DEVICE_ID in client.requested_status_ids
    assert coordinator.data.devices[DEVICE_ID].device_settings["dehumidifier"]["humiditySetpoint"] == 52


async def test_unknown_device_messages_are_replayed_after_discovery(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Messages for unknown devices should be replayed after a hierarchy refresh discovers them."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    await coordinator.async_process_messages(
        LOCATION_ID,
        build_initial_messages(SECOND_DEVICE_ID),
    )

    assert SECOND_DEVICE_ID in coordinator._unknown_device_messages

    coordinator._apply_hierarchy(build_hierarchy(include_second_device=True))
    coordinator._publish_snapshot()

    assert SECOND_DEVICE_ID in coordinator.data.devices
    assert (
        coordinator.data.devices[SECOND_DEVICE_ID]
        .device_settings["dehumidifier"]["humiditySetpoint"]
        == 52
    )


async def test_timeout_rest_refresh_matching_value_succeeds(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A timeout should still succeed if REST settings match the desired value."""
    client = FakeClient()
    client.set_remote_settings(build_device_settings(humidity=55))
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_CONFIRM_TIMEOUT", 0.01
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    await _async_set_target_humidity(coordinator, 55)

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 55
    )


async def test_timeout_rest_refresh_mismatch_reverts_latest_pending_write(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A timeout with mismatched REST settings should revert the optimistic value."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_CONFIRM_TIMEOUT", 0.01
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    with pytest.raises(AprilaireCloudWriteError):
        await _async_set_target_humidity(coordinator, 55)

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 52
    )


async def test_alert_limit_write_uses_nested_settings_confirmation(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Nested alert-limit writes should confirm only on matching DeviceSettings values."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    task = asyncio.create_task(_async_set_alert_limit(coordinator, "highHum", 70))
    await wait_until(
        lambda: (
            coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
                "alertLimits"
            ]["highHum"]
            == 70
        )
    )

    await coordinator.async_process_messages(LOCATION_ID, [build_device_settings(humidity=52)])
    await asyncio.sleep(0)
    assert not task.done()

    settings = build_device_settings(humidity=52)
    settings["dehumidifier"]["alertLimits"]["highHum"] = 70
    await coordinator.async_process_messages(LOCATION_ID, [settings])
    await asyncio.wait_for(task, timeout=1)

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}


async def test_no_supported_devices_issue_created_when_account_has_only_unsupported_devices(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Bootstrap should raise a repair issue when no supported devices are currently available."""

    class UnsupportedOnlyFakeWebSocket(FakeWebSocket):
        async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
            await self._message_callback(
                self._location_id,
                build_initial_messages(control_type="external"),
            )
            await self._state_callback(
                SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
            )
            return True

    created_issues: list[tuple[str, str]] = []
    deleted_issues: list[str] = []

    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.ir.async_create_issue",
        lambda hass, domain, issue_id, **kwargs: created_issues.append(
            (issue_id, kwargs["translation_key"])
        ),
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.ir.async_delete_issue",
        lambda hass, domain, issue_id: deleted_issues.append(issue_id),
    )

    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        UnsupportedOnlyFakeWebSocket,
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    assert coordinator.data.supported_device_ids == ()
    assert (
        coordinator._no_supported_devices_issue_id,
        ISSUE_NO_SUPPORTED_DEVICES,
    ) in created_issues
    assert coordinator._unsupported_devices_issue_id in deleted_issues


async def test_no_supported_devices_issue_clears_when_late_device_setup_becomes_supported(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A no-supported-devices issue should clear as soon as late setup data unlocks support."""

    class UnsupportedOnlyFakeWebSocket(FakeWebSocket):
        async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
            await self._message_callback(
                self._location_id,
                build_initial_messages(control_type="external"),
            )
            await self._state_callback(
                SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
            )
            return True

    deleted_issues: list[str] = []
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.ir.async_create_issue",
        lambda hass, domain, issue_id, **kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.ir.async_delete_issue",
        lambda hass, domain, issue_id: deleted_issues.append(issue_id),
    )

    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        UnsupportedOnlyFakeWebSocket,
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    await coordinator.async_process_messages(
        LOCATION_ID,
        [build_device_setup(control_type="internal")],
    )

    assert coordinator.data.devices[DEVICE_ID].supported is True
    assert coordinator._no_supported_devices_issue_id in deleted_issues
