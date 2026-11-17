"""Runtime failure paths must stop the HA scheduler and receive transport."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.vendor.websocket import AprilaireLocationWebSocket

from .common import FakeClient


async def test_shutdown_stops_base_coordinator_scheduler(hass):
    entry = MockConfigEntry(domain="aprilaire_cloud")
    entry.add_to_hass(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=FakeClient())
    await coordinator.async_shutdown()
    assert coordinator._shutdown_requested


async def test_ping_renewal_failure_closes_receive_transport(monkeypatch):
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.vendor.websocket.WEBSOCKET_PING_INITIAL_DELAY_SECONDS", 0
    )
    client = Mock(async_token_expires_within=AsyncMock(side_effect=RuntimeError("synthetic")))
    manager = AprilaireLocationWebSocket(
        client=client,
        session=Mock(),
        location_id="synthetic",
        message_callback=AsyncMock(),
        state_callback=AsyncMock(),
    )
    ws = Mock(closed=False, close=AsyncMock())
    with pytest.raises(RuntimeError):
        await manager._async_ping_loop(ws)
    ws.close.assert_awaited_once()


async def test_stop_consumes_failed_ping_task():
    manager = AprilaireLocationWebSocket(
        client=Mock(),
        session=Mock(),
        location_id="synthetic",
        message_callback=AsyncMock(),
        state_callback=AsyncMock(),
    )

    async def fail():
        raise RuntimeError("synthetic")

    manager._ping_task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    await manager.async_stop()
    assert manager._ping_task is None


async def test_ping_token_renewal_sends_new_subscription(monkeypatch):
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.vendor.websocket.WEBSOCKET_PING_INITIAL_DELAY_SECONDS", 0
    )
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.vendor.websocket.WEBSOCKET_PING_INTERVAL_SECONDS", 0
    )
    client = Mock(
        async_token_expires_within=AsyncMock(return_value=True),
        async_get_id_token=AsyncMock(return_value="synthetic-token"),
    )
    manager = AprilaireLocationWebSocket(
        client=client,
        session=Mock(),
        location_id="synthetic",
        message_callback=AsyncMock(),
        state_callback=AsyncMock(),
    )
    ws = Mock(closed=False, close=AsyncMock())
    sent = []

    async def send(payload):
        sent.append(payload)
        if payload["action"] == "ping":
            manager._pong_event.set()
            ws.closed = True

    ws.send_json = send
    await manager._async_ping_loop(ws)
    assert sent == [
        {"action": "subscribe", "message": {"token": "synthetic-token", "locationId": "synthetic"}},
        {"action": "ping", "message": ""},
    ]


async def test_canceled_ping_does_not_start_transport_error_recovery():
    manager = AprilaireLocationWebSocket(
        client=Mock(),
        session=Mock(),
        location_id="synthetic",
        message_callback=AsyncMock(),
        state_callback=AsyncMock(),
    )
    ws = Mock(closed=False, close=AsyncMock())
    task = asyncio.create_task(manager._async_ping_loop(ws))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ws.close.assert_not_awaited()
