"""WebSocket support for AprilAire Cloud."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiohttp import ClientSession, WSMessage, WSMsgType

from .api import (
    AprilaireCloudApiClient,
    AprilaireCloudCommunicationError,
)
from .const import (
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
from .models import SocketState

MessageCallback = Callable[[str, list[dict[str, Any]]], Awaitable[None]]
StateCallback = Callable[[SocketState], Awaitable[None]]


class AprilaireWebSocketProtocolError(AprilaireCloudCommunicationError):
    """Raised when the websocket protocol is not understood."""


def decode_websocket_text_frame(raw_text: str) -> list[dict[str, Any]] | None:
    """Decode a websocket text frame into protocol messages."""
    raw_text = raw_text.strip()
    if not raw_text or raw_text in {"pong", '"pong"', "ok", '"ok"'}:
        return None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as err:
        raise AprilaireWebSocketProtocolError("Invalid websocket JSON payload") from err

    if isinstance(data, dict) and data.get("message") in {"Forbidden", "Subscribed"}:
        return [data]

    messages = data if isinstance(data, list) else [data]
    valid_messages = [item for item in messages if isinstance(item, dict)]
    return valid_messages or None


async def async_collect_location_messages(
    *,
    client: AprilaireCloudApiClient,
    session: ClientSession,
    location_id: str,
    initial_timeout: float = WEBSOCKET_INITIAL_SYNC_TIMEOUT,
    idle_timeout: float = WEBSOCKET_INITIAL_IDLE_TIMEOUT,
) -> list[dict[str, Any]]:
    """Collect the initial websocket message burst for a location."""
    token = await client.async_get_id_token()
    messages: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    overall_deadline = loop.time() + initial_timeout
    settle_deadline: float | None = None

    async with session.ws_connect(
        WEBSOCKET_URL,
        heartbeat=None,
        autoping=False,
        receive_timeout=None,
    ) as ws:
        await ws.send_json(
            {"action": "subscribe", "message": {"token": token, "locationId": location_id}}
        )

        while True:
            now = loop.time()
            timeout = max(0.0, overall_deadline - now)
            if settle_deadline is not None:
                timeout = min(timeout, max(0.0, settle_deadline - now))
            if timeout <= 0:
                break

            try:
                message = await ws.receive(timeout=timeout)
            except TimeoutError:
                break

            if message.type is WSMsgType.TEXT:
                decoded = decode_websocket_text_frame(message.data)
                if decoded is None:
                    continue
                if len(decoded) == 1 and decoded[0].get("message") == "Forbidden":
                    token = await client.async_get_id_token(force_refresh=True)
                    await ws.send_json(
                        {
                            "action": "subscribe",
                            "message": {"token": token, "locationId": location_id},
                        }
                    )
                    continue
                if len(decoded) == 1 and decoded[0].get("message") == "Subscribed":
                    continue
                messages.extend(decoded)
                settle_deadline = loop.time() + idle_timeout
                continue

            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                break

            if message.type is WSMsgType.ERROR:
                raise AprilaireCloudCommunicationError(str(ws.exception()))

    return messages


class AprilaireLocationWebSocket:
    """Manage a single location websocket connection."""

    def __init__(
        self,
        *,
        client: AprilaireCloudApiClient,
        session: ClientSession,
        location_id: str,
        message_callback: MessageCallback,
        state_callback: StateCallback,
    ) -> None:
        """Initialize the websocket manager."""
        self._client = client
        self._session = session
        self._location_id = location_id
        self._message_callback = message_callback
        self._state_callback = state_callback
        self._runner_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._pong_event = asyncio.Event()
        self._initial_sync_event = asyncio.Event()
        self._reconnect_attempt = 0
        self._last_error: str | None = None

    async def async_start(self) -> None:
        """Start the websocket loop."""
        if self._runner_task is not None:
            return
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(
            self._async_run(), name=f"aprilaire_ws_{self._location_id}"
        )

    async def async_stop(self) -> None:
        """Stop the websocket loop."""
        LOGGER.debug("WebSocket stopping for location %s", self._location_id)
        self._stop_event.set()
        if self._runner_task is None:
            return
        self._runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._runner_task
        self._runner_task = None

    async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
        """Wait for the initial subscribe burst."""
        try:
            await asyncio.wait_for(self._initial_sync_event.wait(), timeout=wait_timeout)
        except TimeoutError:
            return False
        return True

    async def _async_run(self) -> None:
        """Maintain the websocket connection until stopped."""
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
                    "AprilAire websocket for location %s disconnected: %s",
                    self._location_id,
                    err,
                )
                await self._async_mark_disconnected(err)
                if self._stop_event.is_set():
                    break
                sleep_for = min(delay, WEBSOCKET_RECONNECT_MAX_SECONDS) + random.uniform(0, 1)
                LOGGER.debug(
                    "WebSocket reconnect attempt %d for location %s in %.1fs",
                    self._reconnect_attempt,
                    self._location_id,
                    sleep_for,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * 2, WEBSOCKET_RECONNECT_MAX_SECONDS)

    async def _async_connect_once(self) -> None:
        """Open a websocket connection and process messages until it closes."""
        LOGGER.debug("WebSocket connecting for location %s", self._location_id)
        self._pong_event.clear()
        token = await self._client.async_get_id_token()
        async with self._session.ws_connect(
            WEBSOCKET_URL,
            heartbeat=None,
            autoping=False,
            receive_timeout=None,
        ) as ws:
            await self._async_send_subscribe(ws, token)
            LOGGER.debug("WebSocket subscribed to location %s", self._location_id)
            await self._publish_state(connected=True, initial_sync_complete=False)

            ping_task = asyncio.create_task(self._async_ping_loop(ws))
            try:
                while not self._stop_event.is_set():
                    timeout = (
                        None
                        if self._initial_sync_event.is_set()
                        else WEBSOCKET_INITIAL_SYNC_TIMEOUT
                    )
                    message = await ws.receive(timeout=timeout)
                    await self._async_handle_message(ws, message)
            finally:
                ping_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ping_task
                if self._stop_event.is_set():
                    self._initial_sync_event.clear()
                    await self._publish_state(connected=False, initial_sync_complete=False)

    async def _async_ping_loop(self, ws) -> None:
        """Send the AprilAire application-level ping frames."""
        await asyncio.sleep(WEBSOCKET_PING_INITIAL_DELAY_SECONDS)
        while not self._stop_event.is_set() and not ws.closed:
            if await self._client.async_token_expires_within(WEBSOCKET_PING_INTERVAL_SECONDS + 15):
                token = await self._client.async_get_id_token(force_refresh=True)
                await self._async_send_subscribe(ws, token)

            self._pong_event.clear()
            await ws.send_json({"action": "ping", "message": ""})
            try:
                await asyncio.wait_for(self._pong_event.wait(), WEBSOCKET_PONG_TIMEOUT_SECONDS)
            except TimeoutError:
                LOGGER.warning(
                    "AprilAire websocket for location %s missed pong, reconnecting",
                    self._location_id,
                )
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
        """Process one websocket frame."""
        if message.type is WSMsgType.TEXT:
            raw_text = message.data.strip()
            if raw_text in {"pong", '"pong"'}:
                self._pong_event.set()
                return

            decoded = decode_websocket_text_frame(raw_text)
            if decoded is None:
                return
            if len(decoded) == 1 and decoded[0].get("message") == "Forbidden":
                LOGGER.debug(
                    "AprilAire websocket for location %s requested re-subscribe",
                    self._location_id,
                )
                token = await self._client.async_get_id_token(force_refresh=True)
                await self._async_send_subscribe(ws, token)
                return
            if len(decoded) == 1 and decoded[0].get("message") == "Subscribed":
                return

            await self._message_callback(self._location_id, decoded)
            await self._async_mark_healthy()
            return

        if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
            raise AprilaireCloudCommunicationError("Websocket closed")

        if message.type is WSMsgType.ERROR:
            raise AprilaireCloudCommunicationError(str(ws.exception()))

        raise AprilaireCloudCommunicationError(f"Unexpected websocket frame type: {message.type}")

    async def _publish_state(
        self,
        *,
        connected: bool,
        initial_sync_complete: bool,
    ) -> None:
        """Publish the current connection state."""
        await self._state_callback(
            SocketState(
                location_id=self._location_id,
                connected=connected,
                initial_sync_complete=initial_sync_complete,
                reconnect_attempt=self._reconnect_attempt,
                last_error=self._last_error,
            )
        )

    async def _async_mark_healthy(self) -> None:
        """Mark the websocket as healthy after a successful subscribe/message flow."""
        self._initial_sync_event.set()
        self._reconnect_attempt = 0
        self._last_error = None
        await self._publish_state(connected=True, initial_sync_complete=True)

    async def _async_mark_disconnected(self, err: Exception) -> None:
        """Mark the websocket as disconnected and entering reconnect mode."""
        self._last_error = str(err)
        self._reconnect_attempt += 1
        self._initial_sync_event.clear()
        await self._publish_state(connected=False, initial_sync_complete=False)
