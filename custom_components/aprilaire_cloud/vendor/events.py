"""Typed AprilAire WebSocket protocol frames."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WebSocketFrameKind(StrEnum):
    """Kinds of frames observed at the AprilAire socket boundary."""

    PONG = "pong"
    SUBSCRIBED = "subscribed"
    FORBIDDEN = "forbidden"
    DATA_BATCH = "data_batch"
    PROTOCOL_ERROR = "protocol_error"


@dataclass(frozen=True, slots=True)
class WebSocketFrame:
    """A decoded WebSocket frame with raw vendor dictionaries at the boundary."""

    kind: WebSocketFrameKind
    messages: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None


def decode_websocket_text_frame(raw_text: str) -> WebSocketFrame:
    """Decode text without collapsing acknowledgements or empty data batches."""
    stripped = raw_text.strip()
    if stripped in {"pong", '"pong"'}:
        return WebSocketFrame(WebSocketFrameKind.PONG)
    if stripped in {"ok", '"ok"', '"Subscribed"'}:
        return WebSocketFrame(WebSocketFrameKind.SUBSCRIBED)
    if not stripped:
        return WebSocketFrame(
            WebSocketFrameKind.PROTOCOL_ERROR, error_code="empty_text_frame"
        )

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return WebSocketFrame(WebSocketFrameKind.PROTOCOL_ERROR, error_code="invalid_json")

    if isinstance(data, dict):
        acknowledgement = data.get("message")
        if acknowledgement == "Forbidden":
            return WebSocketFrame(WebSocketFrameKind.FORBIDDEN)
        if acknowledgement == "Subscribed":
            return WebSocketFrame(WebSocketFrameKind.SUBSCRIBED)
        return WebSocketFrame(WebSocketFrameKind.DATA_BATCH, (data,))

    if isinstance(data, list):
        if not all(isinstance(item, dict) for item in data):
            return WebSocketFrame(
                WebSocketFrameKind.PROTOCOL_ERROR,
                error_code="invalid_data_batch",
            )
        return WebSocketFrame(WebSocketFrameKind.DATA_BATCH, tuple(data))

    return WebSocketFrame(
        WebSocketFrameKind.PROTOCOL_ERROR,
        error_code="unexpected_json_type",
    )
