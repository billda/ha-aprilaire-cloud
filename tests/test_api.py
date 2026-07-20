"""API client unit tests for AprilAire Cloud."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError

from custom_components.aprilaire_cloud.const import MAX_RATE_LIMIT_RETRY_SECONDS
from custom_components.aprilaire_cloud.vendor import (
    AprilaireCloudApiClient,
    AprilaireCloudApiError,
    AprilaireCloudAuthenticationError,
    AprilaireCloudCommunicationError,
    AprilaireCloudRateLimitError,
)
from custom_components.aprilaire_cloud.vendor.client import (
    _sanitize_retry_after,
    _sanitized_vendor_code,
)


class FakeResponse:
    """Minimal aiohttp response stub."""

    def __init__(
        self,
        *,
        status: int,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the response."""
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self) -> str:
        """Return the response body."""
        return self._text


class FakeRequestContext:
    """Async context manager used by FakeSession."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        """Initialize the request context."""
        self._response = response
        self._error = error

    async def __aenter__(self) -> FakeResponse:
        """Enter the context."""
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Exit the context."""
        return False


class FakeSession:
    """Minimal session stub for request-path tests."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        """Initialize the session."""
        self._response = response
        self._error = error
        self.request_calls: list[tuple[tuple, dict]] = []

    def request(self, *args, **kwargs) -> FakeRequestContext:
        """Return a fake request context."""
        self.request_calls.append((args, kwargs))
        return FakeRequestContext(response=self._response, error=self._error)


def _build_client(session) -> AprilaireCloudApiClient:
    """Create an API client under test."""
    return AprilaireCloudApiClient("user@example.com", "not-a-real-password", session)


async def test_handle_response_401_refreshes_and_retries(monkeypatch) -> None:
    """A 401 should trigger a forced token refresh and one retry."""
    client = _build_client(object())
    client.async_get_id_token = AsyncMock(return_value="token")  # type: ignore[method-assign]
    client._async_request_json = AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]

    result = await client._async_handle_response(
        FakeResponse(status=401),
        method="GET",
        url="https://example.test/resource",
        payload=None,
        allow_retry=True,
        user_initiated=False,
    )

    assert result == {"ok": True}
    client.async_get_id_token.assert_awaited_once_with(force_refresh=True)
    client._async_request_json.assert_awaited_once_with(
        "GET",
        "https://example.test/resource",
        payload=None,
        route_template="/redacted",
        allow_retry=False,
        user_initiated=False,
    )


async def test_handle_response_401_without_retry_raises_auth_error() -> None:
    """A second 401 should surface as an authentication error."""
    client = _build_client(object())

    with pytest.raises(AprilaireCloudAuthenticationError):
        await client._async_handle_response(
            FakeResponse(status=401),
            method="GET",
            url="https://example.test/resource",
            payload=None,
            allow_retry=False,
            user_initiated=False,
        )


async def test_handle_response_429_sets_rate_limit_deadline() -> None:
    """A 429 should clamp Retry-After and store the backoff deadline."""
    client = _build_client(object())

    with pytest.raises(AprilaireCloudRateLimitError) as err:
        await client._async_handle_response(
            FakeResponse(status=429, headers={"Retry-After": "999"}),
            method="GET",
            url="https://example.test/resource",
            payload=None,
            allow_retry=True,
            user_initiated=False,
        )

    assert err.value.retry_after == float(MAX_RATE_LIMIT_RETRY_SECONDS)
    assert client.rate_limited_until is not None
    assert client.rate_limited_until > datetime.now(tz=UTC)


async def test_patch_device_settings_retries_on_short_rate_limit(monkeypatch) -> None:
    """Short write throttles should retry once automatically."""
    client = _build_client(object())
    request_json = AsyncMock(
        side_effect=[AprilaireCloudRateLimitError(1.0), {}],
    )
    monkeypatch.setattr(client, "_async_request_json", request_json)

    await client.async_patch_device_settings("device-1", {"dehumidifier": {"mode": "on"}})

    assert request_json.await_count == 2


async def test_patch_device_settings_raises_on_long_rate_limit(monkeypatch) -> None:
    """Long write throttles should be surfaced to the caller."""
    client = _build_client(object())
    request_json = AsyncMock(side_effect=AprilaireCloudRateLimitError(10.0))
    monkeypatch.setattr(client, "_async_request_json", request_json)

    with pytest.raises(AprilaireCloudRateLimitError):
        await client.async_patch_device_settings("device-1", {"dehumidifier": {"mode": "on"}})

    assert request_json.await_count == 1


async def test_handle_response_rejects_invalid_json() -> None:
    """Invalid JSON bodies should raise a typed API error."""
    client = _build_client(object())

    with pytest.raises(AprilaireCloudApiError, match="invalid JSON"):
        await client._async_handle_response(
            FakeResponse(status=200, text="not-json"),
            method="GET",
            url="https://example.test/resource",
            payload=None,
            allow_retry=True,
            user_initiated=False,
        )


async def test_request_json_short_circuits_when_still_rate_limited() -> None:
    """Stored local rate limits should fail before opening a new HTTP request."""
    session = FakeSession(
        response=FakeResponse(status=200, text='{"ok": true}'),
    )
    client = _build_client(session)
    client._rate_limited_until = datetime.now(tz=UTC) + timedelta(seconds=30)

    with pytest.raises(AprilaireCloudRateLimitError):
        await client._async_request_json("GET", "https://example.test/resource")

    assert session.request_calls == []


async def test_request_json_wraps_transport_errors() -> None:
    """Transport failures should be mapped without copying exception text."""
    session = FakeSession(error=ClientError("boom"))
    client = _build_client(session)
    client.async_get_id_token = AsyncMock(return_value="token")  # type: ignore[method-assign]

    with pytest.raises(AprilaireCloudCommunicationError) as err:
        await client._async_request_json("GET", "https://example.test/resource")

    assert "boom" not in str(err.value)
    assert "example.test" not in str(err.value)


async def test_http_error_uses_route_template_and_sanitized_vendor_code(caplog) -> None:
    """HTTP failures must not retain the URL or arbitrary response body."""
    client = _build_client(object())
    private_device = "device-private-value"
    private_body = "private server explanation"

    with pytest.raises(AprilaireCloudApiError) as err:
        await client._async_handle_response(
            FakeResponse(
                status=503,
                text=(
                    '{"errorCode":"SERVICE_UNAVAILABLE",'
                    f'"message":"{private_body}"}}'
                ),
            ),
            method="GET",
            url=f"https://example.test/devices/{private_device}/status",
            route_template="/devices/{device_id}/status",
            payload=None,
            allow_retry=True,
            user_initiated=False,
        )

    assert err.value.context is not None
    assert err.value.context.status == 503
    assert err.value.context.method == "GET"
    assert err.value.context.route == "/devices/{device_id}/status"
    assert err.value.context.vendor_code == "SERVICE_UNAVAILABLE"
    combined = f"{err.value} {caplog.text}"
    assert private_device not in combined
    assert private_body not in combined
    assert "example.test" not in combined


def test_rate_limit_and_vendor_code_sanitizers_reject_untrusted_text() -> None:
    """Only bounded machine fields survive response parsing."""
    assert _sanitize_retry_after(None) == 60
    assert _sanitize_retry_after("invalid") == 60
    assert _sanitize_retry_after("0") == 1
    assert _sanitized_vendor_code("not-json") is None
    assert _sanitized_vendor_code("[]") is None
    assert _sanitized_vendor_code('{"message":"private prose"}') is None
    assert _sanitized_vendor_code('{"code":"SAFE_CODE"}') == "SAFE_CODE"
    assert _sanitized_vendor_code('{"code":"unsafe code with spaces"}') is None


async def test_public_client_methods_delegate_to_sanitized_routes() -> None:
    """REST helpers centralize transport behavior and never expose concrete IDs as routes."""
    client = _build_client(object())
    request = AsyncMock(return_value={"ok": True})
    client._async_request_json = request  # type: ignore[method-assign]

    assert client.username == "user@example.com"
    assert client.session is not None
    await client.async_get_user()
    await client.async_get_hierarchy()
    await client.async_get_device_status("device-001")
    await client.async_get_status("device-001", "humidifier")
    await client.async_get_device_settings("device-001")

    assert [call.kwargs["route_template"] for call in request.await_args_list] == [
        "/user",
        "/hierarchy",
        "/devices/{device_id}/status",
        "/devices/{device_id}/status/{status}",
        "/devices/{device_id}/settings",
    ]


async def test_expired_local_backoff_clears_and_empty_response_succeeds() -> None:
    """An expired throttle does not block a normal authenticated request."""
    session = FakeSession(response=FakeResponse(status=200, text=""))
    client = _build_client(session)
    client._rate_limited_until = datetime.now(tz=UTC) - timedelta(seconds=1)
    client.async_get_id_token = AsyncMock(return_value="token")  # type: ignore[method-assign]

    assert await client._async_request_json(
        "GET",
        "https://example.test/resource",
        route_template="/resource",
    ) == {}
    assert client.rate_limited_until is None


async def test_json_array_success_body_is_rejected() -> None:
    """The REST boundary accepts only JSON objects."""
    client = _build_client(object())

    with pytest.raises(AprilaireCloudApiError, match="invalid JSON"):
        await client._async_handle_response(
            FakeResponse(status=200, text="[]"),
            method="GET",
            url="https://example.test/resource",
            payload=None,
            allow_retry=True,
            user_initiated=False,
        )
