"""Per-location AprilAire WebSocket transport."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import ClientSession, WSMessage, WSMsgType

from ..const import (
    LOGGER,
    WEBSOCKET_INITIAL_IDLE_TIMEOUT,
    WEBSOCKET_INITIAL_SYNC_TIMEOUT,
    WEBSOCKET_PING_INITIAL_DELAY_SECONDS,
    WEBSOCKET_PING_INTERVAL_SECONDS,
    WEBSOCKET_PONG_TIMEOUT_SECONDS,
    WEBSOCKET_RECONNECT_MAX_SECONDS,
    WEBSOCKET_RECONNECT_MIN_SECONDS,
    WEBSOCKET_URL,
)
from ..models import SocketState
from .client import AprilaireCloudApiClient, AprilaireCloudCommunicationError
from .events import WebSocketFrameKind, decode_websocket_text_frame

MessageCallback = Callable[[str, list[dict[str, Any]]], Awaitable[None]]
StateCallback = Callable[[SocketState], Awaitable[None]]


class AprilaireWebSocketProtocolError(AprilaireCloudCommunicationError):
    """A sanitized WebSocket protocol failure."""


@dataclass(slots=True)
class _InitialCollectionState:
    """Mutable state for a bounded initial WebSocket collection."""

    overall_deadline: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    settle_deadline: float | None = None
    subscribed: bool = False
    resubscribed: bool = False

    def timeout(self, now: float) -> float:
        """Return the remaining overall/idle timeout."""
        timeout = max(0.0, self.overall_deadline - now)
        if self.settle_deadline is not None:
            timeout = min(timeout, max(0.0, self.settle_deadline - now))
        return timeout

    def mark_subscribed(self, now: float, idle_timeout: float) -> None:
        """Mark protocol progress and start the bounded settle window."""
        self.subscribed = True
        self.settle_deadline = now + idle_timeout


async def _async_send_initial_subscribe(ws, token: str, location_id: str) -> None:
    """Send an initial-collector location subscription."""
    await ws.send_json(
        {"action": "subscribe", "message": {"token": token, "locationId": location_id}}
    )


async def _async_handle_initial_text(
    *,
    ws,
    client: AprilaireCloudApiClient,
    location_id: str,
    data: str,
    state: _InitialCollectionState,
    now: float,
    idle_timeout: float,
) -> None:
    """Apply one text frame to initial collection state."""
    frame = decode_websocket_text_frame(data)
    if frame.kind is WebSocketFrameKind.PONG:
        return
    if frame.kind is WebSocketFrameKind.PROTOCOL_ERROR:
        raise AprilaireWebSocketProtocolError(frame.error_code or "protocol_error")
    if frame.kind is WebSocketFrameKind.FORBIDDEN:
        if state.resubscribed:
            raise AprilaireWebSocketProtocolError("repeated_forbidden")
        state.resubscribed = True
        token = await client.async_get_id_token(force_refresh=True)
        await _async_send_initial_subscribe(ws, token, location_id)
        return
    state.mark_subscribed(now, idle_timeout)
    if frame.kind is not WebSocketFrameKind.SUBSCRIBED:
        state.messages.extend(frame.messages)


def _raise_for_initial_transport_message(message: WSMessage) -> None:
    """Raise a sanitized error for a non-text, non-close frame."""
    if message.type is WSMsgType.ERROR:
        raise AprilaireCloudCommunicationError("websocket_transport_error")
    raise AprilaireWebSocketProtocolError("unexpected_frame_type")


async def async_collect_location_messages(
    *,
    client: AprilaireCloudApiClient,
    session: ClientSession,
    location_id: str,
    initial_timeout: float = WEBSOCKET_INITIAL_SYNC_TIMEOUT,
    idle_timeout: float = WEBSOCKET_INITIAL_IDLE_TIMEOUT,
) -> list[dict[str, Any]]:
    """Collect a bounded initial burst, including a valid empty data batch."""
    token = await client.async_get_id_token()
    loop = asyncio.get_running_loop()
    state = _InitialCollectionState(loop.time() + initial_timeout)

    async with session.ws_connect(
        WEBSOCKET_URL,
        heartbeat=None,
        autoping=False,
        receive_timeout=None,
    ) as ws:
        await _async_send_initial_subscribe(ws, token, location_id)

        while True:
            timeout = state.timeout(loop.time())
            if timeout <= 0:
                break

            try:
                message = await ws.receive(timeout=timeout)
            except TimeoutError:
                break

            if message.type is WSMsgType.TEXT:
                await _async_handle_initial_text(
                    ws=ws,
                    client=client,
                    location_id=location_id,
                    data=message.data,
                    state=state,
                    now=loop.time(),
                    idle_timeout=idle_timeout,
                )
                continue

            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                break
            _raise_for_initial_transport_message(message)

    if not state.subscribed:
        raise AprilaireWebSocketProtocolError("subscription_not_acknowledged")
    return state.messages


class AprilaireLocationWebSocket:
    """Manage one location WebSocket with explicit protocol state."""

    def __init__(
        self,
        *,
        client: AprilaireCloudApiClient,
        session: ClientSession,
        location_id: str,
        message_callback: MessageCallback,
        state_callback: StateCallback,
    ) -> None:
        """Initialize the WebSocket manager."""
        self._client = client
        self._session = session
        self._location_id = location_id
        self._message_callback = message_callback
        self._state_callback = state_callback
        self._runner_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._pong_event = asyncio.Event()
        self._initial_sync_event = asyncio.Event()
        self._reconnect_attempt = 0
        self._last_error: str | None = None
        self._transport_connected = False
        self._subscription_acknowledged = False
        self._last_received_at: datetime | None = None
        self._resubscribed_after_forbidden = False

    async def async_start(self) -> None:
        """Start the WebSocket runner once."""
        if self._runner_task is not None:
            return
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(
            self._async_run(), name="aprilaire_location_websocket"
        )

    async def async_stop(self) -> None:
        """Cancel and await every task owned by this transport."""
        LOGGER.debug("WebSocket stopping")
        self._stop_event.set()
        if self._ping_task is not None:
            self._ping_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None
        if self._runner_task is not None:
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None
        self._transport_connected = False
        self._subscription_acknowledged = False

    async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
        """Wait for a data batch or a bounded subscribed-and-idle settle."""
        try:
            await asyncio.wait_for(self._initial_sync_event.wait(), timeout=wait_timeout)
        except TimeoutError:
            return False
        return True

    async def _async_run(self) -> None:
        """Maintain the connection until stopped."""
        delay = WEBSOCKET_RECONNECT_MIN_SECONDS
        while not self._stop_event.is_set():
            self._initial_sync_event.clear()
            try:
                await self._async_connect_once()
                delay = WEBSOCKET_RECONNECT_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as err:
                LOGGER.warning(
                    "AprilAire WebSocket disconnected (%s)", type(err).__name__
                )
                await self._async_mark_disconnected(err)
                if self._stop_event.is_set():
                    break
                sleep_for = min(delay, WEBSOCKET_RECONNECT_MAX_SECONDS) + random.uniform(0, 1)
                LOGGER.debug(
                    "WebSocket reconnect attempt %d in %.1fs",
                    self._reconnect_attempt,
                    sleep_for,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * 2, WEBSOCKET_RECONNECT_MAX_SECONDS)

    async def _async_connect_once(self) -> None:
        """Open a WebSocket and process frames until it closes."""
        LOGGER.debug("WebSocket connecting")
        self._pong_event.clear()
        self._subscription_acknowledged = False
        self._resubscribed_after_forbidden = False
        token = await self._client.async_get_id_token()
        async with self._session.ws_connect(
            WEBSOCKET_URL,
            heartbeat=None,
            autoping=False,
            receive_timeout=None,
        ) as ws:
            self._transport_connected = True
            await self._async_send_subscribe(ws, token)
            await self._publish_state()
            self._ping_task = asyncio.create_task(
                self._async_ping_loop(ws), name="aprilaire_websocket_ping"
            )
            try:
                while not self._stop_event.is_set():
                    if self._initial_sync_event.is_set():
                        timeout = None
                    elif self._subscription_acknowledged:
                        timeout = WEBSOCKET_INITIAL_IDLE_TIMEOUT
                    else:
                        timeout = WEBSOCKET_INITIAL_SYNC_TIMEOUT
                    try:
                        message = await ws.receive(timeout=timeout)
                    except TimeoutError:
                        if self._subscription_acknowledged:
                            await self._async_mark_healthy()
                            continue
                        raise AprilaireWebSocketProtocolError(
                            "subscription_not_acknowledged"
                        ) from None
                    await self._async_handle_message(ws, message)
            finally:
                if self._ping_task is not None:
                    self._ping_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._ping_task
                    self._ping_task = None
                self._transport_connected = False
                if self._stop_event.is_set():
                    self._initial_sync_event.clear()
                    self._subscription_acknowledged = False
                    await self._publish_state()

    async def _async_ping_loop(self, ws) -> None:
        """Send AprilAire application-level ping frames."""
        await asyncio.sleep(WEBSOCKET_PING_INITIAL_DELAY_SECONDS)
        while not self._stop_event.is_set() and not ws.closed:
            if await self._client.async_token_expires_within(
                WEBSOCKET_PING_INTERVAL_SECONDS + 15
            ):
                token = await self._client.async_get_id_token(force_refresh=True)
                await self._async_send_subscribe(ws, token)

            self._pong_event.clear()
            await ws.send_json({"action": "ping", "message": ""})
            try:
                await asyncio.wait_for(
                    self._pong_event.wait(), WEBSOCKET_PONG_TIMEOUT_SECONDS
                )
            except TimeoutError:
                LOGGER.warning("AprilAire WebSocket missed pong; reconnecting")
                await ws.close()
                return
            await asyncio.sleep(WEBSOCKET_PING_INTERVAL_SECONDS)

    async def _async_send_subscribe(self, ws, token: str) -> None:
        """Subscribe to the current location."""
        await ws.send_json(
            {
                "action": "subscribe",
                "message": {"token": token, "locationId": self._location_id},
            }
        )

    async def _async_handle_message(self, ws, message: WSMessage) -> None:
        """Process one transport frame."""
        if message.type is WSMsgType.TEXT:
            self._last_received_at = datetime.now(tz=UTC)
            frame = decode_websocket_text_frame(message.data)
            if frame.kind is WebSocketFrameKind.PONG:
                self._pong_event.set()
                await self._publish_state()
                return
            if frame.kind is WebSocketFrameKind.PROTOCOL_ERROR:
                raise AprilaireWebSocketProtocolError(
                    frame.error_code or "protocol_error"
                )
            if frame.kind is WebSocketFrameKind.FORBIDDEN:
                if self._resubscribed_after_forbidden:
                    raise AprilaireWebSocketProtocolError("repeated_forbidden")
                self._resubscribed_after_forbidden = True
                self._subscription_acknowledged = False
                token = await self._client.async_get_id_token(force_refresh=True)
                await self._async_send_subscribe(ws, token)
                await self._publish_state()
                return
            if frame.kind is WebSocketFrameKind.SUBSCRIBED:
                self._subscription_acknowledged = True
                await self._publish_state()
                return

            self._subscription_acknowledged = True
            await self._message_callback(self._location_id, list(frame.messages))
            await self._async_mark_healthy()
            return

        if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
            raise AprilaireCloudCommunicationError("websocket_closed")
        if message.type is WSMsgType.ERROR:
            raise AprilaireCloudCommunicationError("websocket_transport_error")
        raise AprilaireWebSocketProtocolError("unexpected_frame_type")

    async def _publish_state(self) -> None:
        """Publish the complete current socket state."""
        await self._state_callback(
            SocketState(
                location_id=self._location_id,
                connected=self._transport_connected,
                initial_sync_complete=self._initial_sync_event.is_set(),
                reconnect_attempt=self._reconnect_attempt,
                last_error=self._last_error,
                subscription_acknowledged=self._subscription_acknowledged,
                last_received_at=self._last_received_at,
            )
        )

    async def _async_mark_healthy(self) -> None:
        """Mark a subscribed data or settled-empty location healthy."""
        self._initial_sync_event.set()
        self._reconnect_attempt = 0
        self._last_error = None
        await self._publish_state()

    async def _async_mark_disconnected(self, err: Exception) -> None:
        """Mark the transport disconnected with a sanitized error code."""
        self._last_error = type(err).__name__
        self._reconnect_attempt += 1
        self._transport_connected = False
        self._subscription_acknowledged = False
        self._initial_sync_event.clear()
        await self._publish_state()
