"""Typed WebSocket protocol and lifecycle tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import WSMsgType

import custom_components.aprilaire_cloud.vendor.websocket as websocket_module
from custom_components.aprilaire_cloud.models import SocketState
from custom_components.aprilaire_cloud.vendor import AprilaireCloudCommunicationError
from custom_components.aprilaire_cloud.vendor.events import (
    WebSocketFrameKind,
    decode_websocket_text_frame,
)
from custom_components.aprilaire_cloud.vendor.websocket import (
    AprilaireLocationWebSocket,
    AprilaireWebSocketProtocolError,
    async_collect_location_messages,
)

from .common import LOCATION_ID


class DummyClient:
    """Minimal client stub for WebSocket state tests."""

    def __init__(self) -> None:
        """Initialize the dummy client."""
        self.force_refresh_calls: list[bool] = []

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return a static synthetic token."""
        self.force_refresh_calls.append(force_refresh)
        return "token-refreshed" if force_refresh else "token"

    async def async_token_expires_within(self, seconds: int) -> bool:
        """Never request an auth refresh."""
        return False


class DummyWebSocket:
    """Minimal WebSocket stub for direct manager tests."""

    def __init__(self) -> None:
        """Initialize the WebSocket."""
        self.sent_json: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Record outbound frames."""
        self.sent_json.append(payload)

    async def close(self) -> None:
        """Close the WebSocket."""
        self.closed = True


class SettledEmptyWebSocket(DummyWebSocket):
    """A subscribed location that produces no device data."""

    def __init__(self) -> None:
        """Initialize scripted receive state."""
        super().__init__()
        self._receive_count = 0
        self._block = asyncio.Event()

    async def __aenter__(self):
        """Enter the socket context."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Exit the socket context."""
        return False

    async def receive(self, **kwargs):
        """Return an ack, settle timeout, then wait for cancellation."""
        self._receive_count += 1
        if self._receive_count == 1:
            return SimpleNamespace(type=WSMsgType.TEXT, data='"Subscribed"')
        if self._receive_count == 2:
            raise TimeoutError
        await self._block.wait()
        raise AssertionError("unreachable")


class DummySession:
    """Session returning one scripted WebSocket."""

    def __init__(self, ws) -> None:
        """Store the socket."""
        self.ws = ws

    def ws_connect(self, *args, **kwargs):
        """Return the scripted socket context."""
        return self.ws


class ScriptedCollectWebSocket(DummyWebSocket):
    """Async context manager returning a finite receive script."""

    def __init__(self, script) -> None:
        """Store messages or exceptions in receive order."""
        super().__init__()
        self.script = list(script)

    async def __aenter__(self):
        """Enter the scripted socket."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Exit without suppressing failures."""
        return False

    async def receive(self, **kwargs):
        """Return or raise the next scripted item."""
        if not self.script:
            raise TimeoutError
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _manager(
    *,
    client=None,
    session=None,
    messages=None,
    states=None,
) -> AprilaireLocationWebSocket:
    """Build a manager with collecting callbacks."""
    collected_messages = messages if messages is not None else []
    collected_states = states if states is not None else []

    async def _message_callback(location_id: str, batch: list[dict[str, Any]]) -> None:
        collected_messages.append(batch)

    async def _state_callback(state: SocketState) -> None:
        collected_states.append(state)

    return AprilaireLocationWebSocket(
        client=client or DummyClient(),
        session=session or object(),
        location_id=LOCATION_ID,
        message_callback=_message_callback,
        state_callback=_state_callback,
    )


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("pong", WebSocketFrameKind.PONG),
        ('"Subscribed"', WebSocketFrameKind.SUBSCRIBED),
        ('{"message":"Forbidden"}', WebSocketFrameKind.FORBIDDEN),
        ("[]", WebSocketFrameKind.DATA_BATCH),
        ('[{"_type":"RefreshEvent"}]', WebSocketFrameKind.DATA_BATCH),
        ("not-json", WebSocketFrameKind.PROTOCOL_ERROR),
        ('["invalid"]', WebSocketFrameKind.PROTOCOL_ERROR),
    ],
)
def test_decoder_returns_typed_frames(raw: str, kind: WebSocketFrameKind) -> None:
    """Every observed/control case remains distinguishable after decoding."""
    assert decode_websocket_text_frame(raw).kind is kind


def test_empty_array_is_a_real_empty_data_batch() -> None:
    """An empty location frame must not be collapsed into no frame."""
    frame = decode_websocket_text_frame("[]")

    assert frame.kind is WebSocketFrameKind.DATA_BATCH
    assert frame.messages == ()


async def test_websocket_reconnect_state_resets_after_recovery() -> None:
    """Reconnect metadata clears after subscribed data becomes healthy."""
    states: list[SocketState] = []
    manager = _manager(states=states)

    await manager._async_mark_disconnected(RuntimeError("private source detail"))

    assert states[-1].transport_connected is False
    assert states[-1].initial_sync_complete is False
    assert states[-1].reconnect_attempt == 1
    assert states[-1].last_error == "RuntimeError"

    manager._transport_connected = True
    manager._subscription_acknowledged = True
    await manager._async_mark_healthy()

    assert states[-1].transport_connected is True
    assert states[-1].subscription_acknowledged is True
    assert states[-1].initial_sync_complete is True
    assert states[-1].reconnect_attempt == 0
    assert states[-1].last_error is None


async def test_empty_data_batch_completes_initial_sync() -> None:
    """A decoded empty batch is sufficient initial data for a location."""
    messages: list[list[dict[str, Any]]] = []
    states: list[SocketState] = []
    manager = _manager(messages=messages, states=states)
    manager._transport_connected = True

    await manager._async_handle_message(
        DummyWebSocket(),
        SimpleNamespace(type=WSMsgType.TEXT, data="[]"),
    )

    assert messages == [[]]
    assert states[-1].initial_sync_complete is True
    assert states[-1].subscription_acknowledged is True
    assert states[-1].last_received_at is not None


async def test_subscribed_empty_location_settles_without_reconnect(monkeypatch) -> None:
    """An ack followed by bounded idle time is a healthy empty location."""
    states: list[SocketState] = []
    ws = SettledEmptyWebSocket()
    manager = _manager(session=DummySession(ws), states=states)
    monkeypatch.setattr(websocket_module, "WEBSOCKET_INITIAL_IDLE_TIMEOUT", 0.01)
    monkeypatch.setattr(websocket_module, "WEBSOCKET_PING_INITIAL_DELAY_SECONDS", 60)

    await manager.async_start()
    assert await manager.async_wait_for_initial_sync(0.2) is True

    assert states[-1].transport_connected is True
    assert states[-1].subscription_acknowledged is True
    assert states[-1].initial_sync_complete is True
    assert states[-1].reconnect_attempt == 0

    await manager.async_stop()
    assert manager._runner_task is None
    assert manager._ping_task is None


async def test_forbidden_message_refreshes_and_resubscribes_once() -> None:
    """One Forbidden frame forces one renewal and one re-subscribe."""
    client = DummyClient()
    ws = DummyWebSocket()
    manager = _manager(client=client)

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
    with pytest.raises(AprilaireWebSocketProtocolError, match="repeated_forbidden"):
        await manager._async_handle_message(
            ws,
            SimpleNamespace(type=WSMsgType.TEXT, data='{"message":"Forbidden"}'),
        )


async def test_ping_loop_closes_socket_when_pong_is_missed(monkeypatch) -> None:
    """A missed application pong closes the socket for reconnect."""
    ws = DummyWebSocket()
    manager = _manager()
    monkeypatch.setattr(websocket_module, "WEBSOCKET_PING_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(websocket_module, "WEBSOCKET_PONG_TIMEOUT_SECONDS", 0.01)

    await manager._async_ping_loop(ws)

    assert ws.sent_json == [{"action": "ping", "message": ""}]
    assert ws.closed is True


async def test_bounded_collector_accepts_subscribed_empty_location() -> None:
    """The config-flow collector treats acknowledged idle as a valid empty burst."""
    client = DummyClient()
    ws = ScriptedCollectWebSocket(
        [
            SimpleNamespace(type=WSMsgType.TEXT, data='"Subscribed"'),
            TimeoutError(),
        ]
    )

    result = await async_collect_location_messages(
        client=client,
        session=DummySession(ws),  # type: ignore[arg-type]
        location_id=LOCATION_ID,
        initial_timeout=1,
        idle_timeout=0.01,
    )

    assert result == []
    assert ws.sent_json[0]["action"] == "subscribe"


async def test_bounded_collector_handles_pong_data_and_one_forbidden() -> None:
    """Control frames do not erase data and Forbidden renews exactly once."""
    client = DummyClient()
    ws = ScriptedCollectWebSocket(
        [
            SimpleNamespace(type=WSMsgType.TEXT, data="pong"),
            SimpleNamespace(type=WSMsgType.TEXT, data='{"message":"Forbidden"}'),
            SimpleNamespace(
                type=WSMsgType.TEXT,
                data='[{"_type":"RefreshEvent"}]',
            ),
            TimeoutError(),
        ]
    )

    result = await async_collect_location_messages(
        client=client,
        session=DummySession(ws),  # type: ignore[arg-type]
        location_id=LOCATION_ID,
        initial_timeout=1,
        idle_timeout=0.01,
    )

    assert result == [{"_type": "RefreshEvent"}]
    assert client.force_refresh_calls == [False, True]
    assert len(ws.sent_json) == 2


@pytest.mark.parametrize(
    ("script", "error_type", "match"),
    [
        (
            [
                SimpleNamespace(
                    type=WSMsgType.TEXT,
                    data='{"message":"Forbidden"}',
                ),
                SimpleNamespace(
                    type=WSMsgType.TEXT,
                    data='{"message":"Forbidden"}',
                ),
            ],
            AprilaireWebSocketProtocolError,
            "repeated_forbidden",
        ),
        (
            [SimpleNamespace(type=WSMsgType.TEXT, data="not-json")],
            AprilaireWebSocketProtocolError,
            "invalid_json",
        ),
        (
            [SimpleNamespace(type=WSMsgType.ERROR)],
            AprilaireCloudCommunicationError,
            "websocket_transport_error",
        ),
        (
            [SimpleNamespace(type=WSMsgType.BINARY)],
            AprilaireWebSocketProtocolError,
            "unexpected_frame_type",
        ),
    ],
)
async def test_bounded_collector_rejects_protocol_failures(
    script,
    error_type,
    match,
) -> None:
    """The bounded collector classifies malformed transport states."""
    with pytest.raises(error_type, match=match):
        await async_collect_location_messages(
            client=DummyClient(),
            session=DummySession(ScriptedCollectWebSocket(script)),  # type: ignore[arg-type]
            location_id=LOCATION_ID,
            initial_timeout=1,
            idle_timeout=0.01,
        )


async def test_bounded_collector_requires_subscription_acknowledgment() -> None:
    """A close before any protocol acknowledgment is not a valid empty location."""
    ws = ScriptedCollectWebSocket(
        [SimpleNamespace(type=WSMsgType.CLOSE)]
    )

    with pytest.raises(
        AprilaireWebSocketProtocolError,
        match="subscription_not_acknowledged",
    ):
        await async_collect_location_messages(
            client=DummyClient(),
            session=DummySession(ws),  # type: ignore[arg-type]
            location_id=LOCATION_ID,
            initial_timeout=1,
            idle_timeout=0.01,
        )


async def test_manager_control_and_transport_frames_are_typed() -> None:
    """Manager control, close, error, and unexpected frames keep distinct outcomes."""
    states: list[SocketState] = []
    manager = _manager(states=states)
    ws = DummyWebSocket()

    await manager._async_handle_message(
        ws,
        SimpleNamespace(type=WSMsgType.TEXT, data="pong"),
    )
    assert manager._pong_event.is_set()
    await manager._async_handle_message(
        ws,
        SimpleNamespace(type=WSMsgType.TEXT, data='"Subscribed"'),
    )
    assert manager._subscription_acknowledged is True
    with pytest.raises(AprilaireWebSocketProtocolError, match="invalid_json"):
        await manager._async_handle_message(
            ws,
            SimpleNamespace(type=WSMsgType.TEXT, data="not-json"),
        )
    with pytest.raises(AprilaireCloudCommunicationError, match="websocket_closed"):
        await manager._async_handle_message(
            ws,
            SimpleNamespace(type=WSMsgType.CLOSE),
        )
    with pytest.raises(
        AprilaireCloudCommunicationError,
        match="websocket_transport_error",
    ):
        await manager._async_handle_message(
            ws,
            SimpleNamespace(type=WSMsgType.ERROR),
        )
    with pytest.raises(AprilaireWebSocketProtocolError, match="unexpected_frame_type"):
        await manager._async_handle_message(
            ws,
            SimpleNamespace(type=WSMsgType.BINARY),
        )


async def test_start_is_idempotent() -> None:
    """Starting an already-running manager cannot create a second runner."""
    manager = _manager(session=DummySession(SettledEmptyWebSocket()))

    await manager.async_start()
    first = manager._runner_task
    await manager.async_start()

    assert manager._runner_task is first
    await manager.async_stop()


async def test_connect_without_ack_is_protocol_failure(monkeypatch) -> None:
    """Initial idle without a subscription acknowledgment forces reconnect."""
    ws = ScriptedCollectWebSocket([TimeoutError()])
    manager = _manager(session=DummySession(ws))
    monkeypatch.setattr(websocket_module, "WEBSOCKET_PING_INITIAL_DELAY_SECONDS", 60)

    with pytest.raises(
        AprilaireWebSocketProtocolError,
        match="subscription_not_acknowledged",
    ):
        await manager._async_connect_once()
