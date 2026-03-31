"""Websocket lifecycle tests for AprilAire Cloud."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiohttp import WSMsgType

import custom_components.aprilaire_cloud.websocket as websocket_module
from custom_components.aprilaire_cloud.models import SocketState
from custom_components.aprilaire_cloud.websocket import (
    AprilaireLocationWebSocket,
    AprilaireWebSocketProtocolError,
    decode_websocket_text_frame,
)

from .common import LOCATION_ID


class DummyClient:
    """Minimal client stub for websocket state tests."""

    def __init__(self) -> None:
        """Initialize the dummy client."""
        self.force_refresh_calls: list[bool] = []

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return a static token."""
        self.force_refresh_calls.append(force_refresh)
        return "token-refreshed" if force_refresh else "token"

    async def async_token_expires_within(self, seconds: int) -> bool:
        """Never request an auth refresh."""
        return False


class DummyWebSocket:
    """Minimal websocket stub for direct manager tests."""

    def __init__(self) -> None:
        """Initialize the websocket."""
        self.sent_json: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Record outbound websocket frames."""
        self.sent_json.append(payload)

    async def close(self) -> None:
        """Close the websocket."""
        self.closed = True


async def test_websocket_reconnect_state_resets_after_recovery() -> None:
    """Reconnect metadata should clear after the socket becomes healthy again."""
    states: list[SocketState] = []

    async def _message_callback(location_id: str, messages: list[dict[str, Any]]) -> None:
        return None

    async def _state_callback(state: SocketState) -> None:
        states.append(state)

    manager = AprilaireLocationWebSocket(
        client=DummyClient(),
        session=object(),
        location_id=LOCATION_ID,
        message_callback=_message_callback,
        state_callback=_state_callback,
    )

    await manager._async_mark_disconnected(RuntimeError("socket dropped"))

    assert states[-1].connected is False
    assert states[-1].initial_sync_complete is False
    assert states[-1].reconnect_attempt == 1
    assert states[-1].last_error == "socket dropped"

    await manager._async_mark_healthy()

    assert states[-1].connected is True
    assert states[-1].initial_sync_complete is True
    assert states[-1].reconnect_attempt == 0
    assert states[-1].last_error is None


def test_decode_websocket_text_frame_ignores_ack_frames_and_rejects_invalid_json() -> None:
    """Ack frames should be ignored and invalid JSON should surface as a protocol error."""
    assert decode_websocket_text_frame("ok") is None
    assert decode_websocket_text_frame('"Subscribed"') is None

    try:
        decode_websocket_text_frame("not-json")
    except AprilaireWebSocketProtocolError:
        pass
    else:
        raise AssertionError("Expected invalid websocket JSON to raise a protocol error")


async def test_forbidden_message_triggers_re_subscribe_with_refreshed_token() -> None:
    """A live Forbidden frame should force a token refresh and re-subscribe."""
    client = DummyClient()
    ws = DummyWebSocket()

    async def _message_callback(location_id: str, messages: list[dict[str, Any]]) -> None:
        return None

    async def _state_callback(state: SocketState) -> None:
        return None

    manager = AprilaireLocationWebSocket(
        client=client,
        session=object(),
        location_id=LOCATION_ID,
        message_callback=_message_callback,
        state_callback=_state_callback,
    )

    await manager._async_handle_message(
        ws,
        SimpleNamespace(type=WSMsgType.TEXT, data='{"message":"Forbidden"}'),
    )

    assert client.force_refresh_calls == [True]
    assert ws.sent_json == [
        {
            "action": "subscribe",
            "message": {"token": "token-refreshed", "locationId": LOCATION_ID},
        }
    ]


async def test_ping_loop_closes_socket_when_pong_is_missed(monkeypatch) -> None:
    """The websocket ping loop should close the connection after a missed pong."""
    client = DummyClient()
    ws = DummyWebSocket()

    async def _message_callback(location_id: str, messages: list[dict[str, Any]]) -> None:
        return None

    async def _state_callback(state: SocketState) -> None:
        return None

    manager = AprilaireLocationWebSocket(
        client=client,
        session=object(),
        location_id=LOCATION_ID,
        message_callback=_message_callback,
        state_callback=_state_callback,
    )

    monkeypatch.setattr(websocket_module, "WEBSOCKET_PING_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(websocket_module, "WEBSOCKET_PONG_TIMEOUT_SECONDS", 0.01)

    await manager._async_ping_loop(ws)

    assert ws.sent_json == [{"action": "ping", "message": ""}]
    assert ws.closed is True
