"""Coordinator tests for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.api import (
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)
from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.models import SocketState

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    USERNAME,
    build_dehumidifier_status,
    build_device_settings,
    build_hierarchy,
    build_initial_messages,
    build_user,
    deep_copy,
)


class FakeClient:
    """Minimal fake API client for coordinator tests."""

    def __init__(self) -> None:
        """Initialize the fake client."""
        self.username = USERNAME
        self.session = object()
        self._hierarchy = build_hierarchy()
        self._rate_limit = False
        self.device_settings = build_device_settings()
        self.patched_payloads: list[dict] = []
        self.patch_started = asyncio.Event()
        self.patch_release: asyncio.Event | None = None
        self.patch_side_effect: Exception | None = None

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
        return deep_copy(self.device_settings)

    async def async_patch_device_settings(self, device_id: str, payload: dict) -> None:
        """Pretend a write succeeded."""
        self.patched_payloads.append(deep_copy(payload))
        self.patch_started.set()
        if self.patch_release is not None:
            await self.patch_release.wait()
        if self.patch_side_effect is not None:
            raise self.patch_side_effect
        return None

    def set_remote_settings(self, payload: dict) -> None:
        """Update the fake remote settings payload."""
        self.device_settings = deep_copy(payload)


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

    async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
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


async def wait_until(predicate, *, wait_timeout: float = 1.0) -> None:
    """Wait until a predicate becomes true."""
    end = asyncio.get_running_loop().time() + wait_timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Timed out waiting for predicate")


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

    task = asyncio.create_task(coordinator.async_set_target_humidity(DEVICE_ID, 55))
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

    first = asyncio.create_task(coordinator.async_set_target_humidity(DEVICE_ID, 45))
    await wait_until(lambda: len(client.patched_payloads) == 1)

    second = asyncio.create_task(coordinator.async_set_target_humidity(DEVICE_ID, 50))
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

    await coordinator.async_set_target_humidity(DEVICE_ID, 55)

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
        await coordinator.async_set_target_humidity(DEVICE_ID, 55)

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

    task = asyncio.create_task(coordinator.async_set_alert_limit(DEVICE_ID, "highHum", 70))
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
