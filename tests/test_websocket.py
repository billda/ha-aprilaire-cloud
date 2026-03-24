"""Websocket lifecycle tests for AprilAire Cloud."""

from __future__ import annotations

from typing import Any

from custom_components.aprilaire_cloud.models import SocketState
from custom_components.aprilaire_cloud.websocket import AprilaireLocationWebSocket

from .common import LOCATION_ID


class DummyClient:
    """Minimal client stub for websocket state tests."""

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return a static token."""
        return "token"

    async def async_token_expires_within(self, seconds: int) -> bool:
        """Never request an auth refresh."""
        return False


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
