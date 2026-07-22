"""Coordinator tests for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.const import (
    DOMAIN,
    ISSUE_NO_SUPPORTED_DEVICES,
)
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.models import SocketState
from custom_components.aprilaire_cloud.profiles import (
    CommandValidationError,
    SetDehumidifierTarget,
    SetHighHumidityAlert,
)
from custom_components.aprilaire_cloud.vendor import (
    ApiErrorContext,
    AprilaireCloudApiError,
    AprilaireCloudAuthenticationTransientError,
    AprilaireCloudCommunicationError,
    AprilaireCloudInvalidCredentialsError,
    AuthOperation,
)

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    SECOND_DEVICE_ID,
    SECOND_LOCATION_ID,
    THERMOSTAT_DEVICE_ID,
    USERNAME,
    FakeClient,
    FakeWebSocket,
    MultiLocationFakeWebSocket,
    UnavailableWebSocket,
    bootstrap_coordinator,
    build_dehumidifier_status,
    build_device_settings,
    build_device_setup,
    build_device_status,
    build_hierarchy,
    build_initial_messages,
    build_thermostat_hierarchy,
    build_thermostat_settings,
    build_two_location_hierarchy,
    build_user,
    wait_until,
)


async def test_thermostat_cold_start_hydrates_after_settings_classification(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """REST settings classify a thermostat before optional routes are planned."""
    client = FakeClient()
    client._hierarchy = build_thermostat_hierarchy()
    client.device_settings = build_thermostat_settings()
    client.device_settings["humidifier"] = {
        "mode": "on",
        "humiditySetpoint": 40,
    }
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        UnavailableWebSocket,
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )

    await coordinator._async_setup()

    record = coordinator.data.devices[THERMOSTAT_DEVICE_ID]
    assert record.supported is True
    assert record.profile_key == "thermostat"
    endpoints = [endpoint for _, endpoint in client.requested_status_endpoints]
    assert "dehumidifier" not in endpoints
    assert {"thermostat/PZ1", "thermostat/SZ2", "thermostat/SZ3"}.issubset(endpoints)


async def test_cancelled_rest_wait_does_not_create_unawaited_request(
    hass,
    enable_custom_integrations,
    config_entry,
) -> None:
    """Cancellation before semaphore entry must not instantiate a request coroutine."""
    client = FakeClient()
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    semaphore = asyncio.Semaphore(0)
    request_factory = AsyncMock(return_value={})

    task = asyncio.create_task(
        coordinator._capture_rest_result(semaphore, request_factory)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    request_factory.assert_not_called()


async def test_optional_iaq_404_preserves_data_and_is_conservatively_cached(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """One unsupported optional endpoint cannot invalidate thermostat hydration."""
    client = FakeClient()
    client._hierarchy = build_thermostat_hierarchy()
    client.device_settings = build_thermostat_settings()
    client.device_settings["humidifier"] = {
        "mode": "on",
        "humiditySetpoint": 40,
    }
    client.rest_failures[
        ("status", f"{THERMOSTAT_DEVICE_ID}:humidifier")
    ] = AprilaireCloudApiError(
        context=ApiErrorContext(
            status=404,
            method="GET",
            route="/devices/{device_id}/status/{status}",
        )
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        UnavailableWebSocket,
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )

    await coordinator._async_setup()
    await coordinator._async_rest_refresh_devices({THERMOSTAT_DEVICE_ID})

    record = coordinator.data.devices[THERMOSTAT_DEVICE_ID]
    assert record.supported is True
    assert record.device_settings
    assert record.device_status["model"] == "8920W_GS"
    assert "thermostatPZ1" in record.status_payloads
    humidifier_calls = [
        call
        for call in client.requested_status_endpoints
        if call == (THERMOSTAT_DEVICE_ID, "humidifier")
    ]
    assert len(humidifier_calls) == 1

    client.device_settings["asOf"] = "2026-03-24T00:10:00.000Z"
    client.device_settings["humidifier"]["humiditySetpoint"] = 41
    await coordinator._async_rest_refresh_devices({THERMOSTAT_DEVICE_ID})
    humidifier_calls = [
        call
        for call in client.requested_status_endpoints
        if call == (THERMOSTAT_DEVICE_ID, "humidifier")
    ]
    assert len(humidifier_calls) == 2


async def test_one_critical_device_failure_preserves_another_device_success(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """Independent hydration keeps successful device data."""
    client = FakeClient()
    client._hierarchy = build_hierarchy(include_second_device=True)
    failure = AprilaireCloudCommunicationError()
    client.rest_failures[("device_status", SECOND_DEVICE_ID)] = failure
    client.rest_failures[("device_settings", SECOND_DEVICE_ID)] = failure
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        UnavailableWebSocket,
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )

    await coordinator._async_setup()

    assert coordinator.data.devices[DEVICE_ID].device_settings
    assert coordinator.data.devices[DEVICE_ID].device_status
    assert SECOND_DEVICE_ID in coordinator.data.devices
    assert coordinator.data.devices[SECOND_DEVICE_ID].device_settings == {}


async def test_shutdown_cancels_and_awaits_owned_tasks(
    hass,
    enable_custom_integrations,
    config_entry,
) -> None:
    """Coordinator shutdown owns refresh-event and write-reconciliation tasks."""
    client = FakeClient()
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def _blocked_refresh() -> None:
        started.set()
        await blocked.wait()

    coordinator.async_request_refresh = _blocked_refresh  # type: ignore[method-assign]
    coordinator._schedule_refresh()
    reconciliation_task = hass.async_create_task(blocked.wait())
    coordinator._write_reconciliation_tasks.add(reconciliation_task)
    await asyncio.wait_for(started.wait(), timeout=1)

    await coordinator.async_shutdown()

    assert coordinator._refresh_event_task is None
    assert reconciliation_task.cancelled()
    assert coordinator._write_reconciliation_tasks == set()


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
    await coordinator.async_execute_command(
        DEVICE_ID,
        SetDehumidifierTarget(humidity=humidity),
    )


async def test_command_validation_precedes_optimistic_state(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """An invalid command cannot mutate pending state or reach the client."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    before = coordinator.data.devices[DEVICE_ID]
    with pytest.raises(CommandValidationError):
        await coordinator.async_execute_command(
            DEVICE_ID,
            SetDehumidifierTarget(humidity=1),
        )

    assert client.patched_payloads == []
    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == (
        before.pending_device_settings
    )


async def _async_set_alert_limit(
    coordinator: AprilaireCloudDataUpdateCoordinator,
    key: str,
    value: int,
) -> None:
    """Write a dehumidifier alert limit."""
    assert key == "highHum"
    await coordinator.async_execute_command(
        DEVICE_ID,
        SetHighHumidityAlert(humidity=value),
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


async def test_transient_auth_outage_is_retryable_not_reauth(
    hass,
    enable_custom_integrations,
    config_entry,
) -> None:
    """A Cognito outage during setup must remain an UpdateFailed retry."""
    client = FakeClient()
    client.async_get_user = AsyncMock(
        side_effect=AprilaireCloudAuthenticationTransientError(
            "ServiceUnavailableException", AuthOperation.FULL_LOGIN
        )
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_setup()

    assert "ServiceUnavailableException" in str(err.value)
    assert not isinstance(err.value.__cause__, ConfigEntryAuthFailed)


async def test_definite_invalid_credentials_start_reauth(
    hass,
    enable_custom_integrations,
    config_entry,
) -> None:
    """A definite full-login rejection maps to ConfigEntryAuthFailed."""
    client = FakeClient()
    client.async_get_user = AsyncMock(
        side_effect=AprilaireCloudInvalidCredentialsError(
            "NotAuthorizedException", AuthOperation.FULL_LOGIN
        )
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_setup()


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
                "asOf": "2026-03-24T00:10:00.000Z",
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
                        "type": "ventilator",
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
    remote_settings = build_device_settings(humidity=55)
    remote_settings["asOf"] = "2026-03-24T00:10:00.000Z"
    client.set_remote_settings(remote_settings)
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


async def test_accepted_write_reconciles_in_background_without_false_service_error(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A successful PATCH is accepted while delayed observation reconciles later."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_CONFIRM_TIMEOUT", 0.01
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_RECONCILIATION_DELAY_SECONDS",
        0,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator."
        "POST_WRITE_DEFERRED_RECONCILIATION_DELAYS_SECONDS",
        (0,),
    )

    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    await _async_set_target_humidity(coordinator, 55)
    await asyncio.gather(*coordinator._write_reconciliation_tasks)

    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 52
    )


async def test_deferred_reconciliation_observes_late_success(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A late remote observation clears optimistic state without an HA error."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_CONFIRM_TIMEOUT",
        0.01,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_RECONCILIATION_DELAY_SECONDS",
        0,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator."
        "POST_WRITE_DEFERRED_RECONCILIATION_DELAYS_SECONDS",
        (0,),
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)
    stale = build_device_settings(humidity=52)
    stale["asOf"] = "2026-03-24T00:10:00.000Z"
    confirmed = build_device_settings(humidity=55)
    confirmed["asOf"] = "2026-03-24T00:11:00.000Z"
    client.async_get_device_settings = AsyncMock(
        side_effect=[stale, stale, confirmed],
    )

    await _async_set_target_humidity(coordinator, 55)
    await asyncio.gather(*coordinator._write_reconciliation_tasks)

    assert client.async_get_device_settings.await_count == 3
    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}
    assert (
        coordinator.data.devices[DEVICE_ID].effective_device_settings["dehumidifier"][
            "humiditySetpoint"
        ]
        == 55
    )


async def test_authoritative_rest_settings_signal_inflight_confirmation(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A concurrent authoritative refresh should wake the confirmation waiter."""
    client = FakeClient()
    client.patch_release = asyncio.Event()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)

    task = asyncio.create_task(_async_set_target_humidity(coordinator, 55))
    await asyncio.wait_for(client.patch_started.wait(), timeout=1)
    client.patch_release.set()
    await asyncio.sleep(0)
    confirmed = build_device_settings(humidity=55)
    confirmed["asOf"] = "2026-03-24T00:10:00.000Z"
    coordinator._apply_full_device_settings(DEVICE_ID, confirmed)
    coordinator._publish_snapshot()

    await asyncio.wait_for(task, timeout=1)
    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}


async def test_delayed_rest_observation_confirms_on_second_bounded_check(
    hass,
    enable_custom_integrations,
    monkeypatch,
    config_entry,
) -> None:
    """A successful PATCH may take one bounded REST cycle to become visible."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_CONFIRM_TIMEOUT",
        0.01,
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.POST_WRITE_RECONCILIATION_DELAY_SECONDS",
        0,
    )
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=config_entry, client=client
    )
    await bootstrap_coordinator(coordinator)
    stale = build_device_settings(humidity=52)
    stale["asOf"] = "2026-03-24T00:10:00.000Z"
    confirmed = build_device_settings(humidity=55)
    confirmed["asOf"] = "2026-03-24T00:11:00.000Z"
    client.async_get_device_settings = AsyncMock(
        side_effect=[stale, confirmed],
    )

    await _async_set_target_humidity(coordinator, 55)

    assert client.async_get_device_settings.await_count == 2
    assert coordinator.data.devices[DEVICE_ID].pending_device_settings == {}


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
            messages = build_initial_messages()
            messages[2] = {
                "_type": "DeviceSetup",
                "deviceId": DEVICE_ID,
                "asOf": "2026-03-24T00:00:02.000Z",
                "type": "ventilator",
            }
            await self._message_callback(
                self._location_id,
                messages,
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
