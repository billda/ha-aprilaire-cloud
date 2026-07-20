"""AprilAire REST transport."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from socket import gaierror
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from ..const import (
    ACCOUNT_API,
    DEFAULT_RATE_LIMIT_RETRY_SECONDS,
    DEFAULT_REQUEST_TIMEOUT,
    DEVICE_API,
    LOGGER,
    MAX_RATE_LIMIT_RETRY_SECONDS,
    SHORT_WRITE_RETRY_THRESHOLD_SECONDS,
)
from .auth import (
    AprilaireCloudAuthenticationTransientError,
    AuthOperation,
    CognitoAuthProvider,
)


@dataclass(frozen=True, slots=True)
class ApiErrorContext:
    """Sanitized context for an AprilAire HTTP failure."""

    status: int | None = None
    method: str | None = None
    route: str | None = None
    vendor_code: str | None = None


class AprilaireCloudApiError(Exception):
    """Base API error with optional value-free request context."""

    def __init__(
        self,
        message: str = "AprilAire API request failed",
        *,
        context: ApiErrorContext | None = None,
    ) -> None:
        """Initialize an API error."""
        self.context = context
        if context is not None:
            parts = [
                part
                for part in (
                    context.method,
                    context.route,
                    f"HTTP {context.status}" if context.status is not None else None,
                    f"code={context.vendor_code}" if context.vendor_code else None,
                )
                if part
            ]
            message += f" ({', '.join(parts)})" if parts else ""
        super().__init__(message)


class AprilaireCloudCommunicationError(AprilaireCloudApiError):
    """Network or transport failure."""


@dataclass(slots=True)
class AprilaireCloudRateLimitError(AprilaireCloudApiError):
    """A request was throttled."""

    retry_after: float


class AprilaireCloudWriteError(AprilaireCloudApiError):
    """A requested write could not be confirmed."""


def _sanitize_retry_after(value: str | None) -> float:
    """Clamp a retry delay to a reasonable range."""
    if value is None:
        return DEFAULT_RATE_LIMIT_RETRY_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_RATE_LIMIT_RETRY_SECONDS
    return max(1.0, min(parsed, float(MAX_RATE_LIMIT_RETRY_SECONDS)))


def _sanitized_vendor_code(body: str) -> str | None:
    """Extract a short machine code without retaining arbitrary response text."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("code", "errorCode", "error", "__type"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:#-]{1,64}", candidate):
            return candidate
    return None


class AprilaireCloudApiClient:
    """AprilAire Cloud REST client."""

    def __init__(
        self,
        username: str,
        password: str,
        session: ClientSession,
        *,
        auth_provider: CognitoAuthProvider | None = None,
    ) -> None:
        """Initialize the client."""
        self._username = username
        self._session = session
        self._auth = auth_provider or CognitoAuthProvider(username, password)
        self._rate_limited_until: datetime | None = None

    @property
    def username(self) -> str:
        """Return the configured account username."""
        return self._username

    @property
    def session(self) -> ClientSession:
        """Return the shared aiohttp client session."""
        return self._session

    @property
    def rate_limited_until(self) -> datetime | None:
        """Return the current REST throttle deadline."""
        return self._rate_limited_until

    @property
    def auth_metadata(self) -> dict[str, str | None]:
        """Return token-free authentication lifecycle diagnostics."""
        return self._auth.metadata.as_diagnostics()

    async def async_authenticate(self) -> None:
        """Perform a full Cognito authentication."""
        await self._auth.async_authenticate()

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid ID token."""
        return await self._auth.async_get_id_token(force_refresh=force_refresh)

    async def async_token_expires_within(self, seconds: int) -> bool:
        """Return whether the ID token expires within the given interval."""
        return await self._auth.async_token_expires_within(seconds)

    async def async_get_user(self) -> dict[str, Any]:
        """Fetch the authenticated user profile."""
        return await self._async_request_json(
            "GET", f"{ACCOUNT_API}/user", route_template="/user"
        )

    async def async_get_hierarchy(self) -> dict[str, Any]:
        """Fetch the full hierarchy."""
        return await self._async_request_json(
            "GET", f"{DEVICE_API}/hierarchy", route_template="/hierarchy"
        )

    async def async_get_device_status(self, device_id: str) -> dict[str, Any]:
        """Fetch device status."""
        return await self._async_request_json(
            "GET",
            f"{DEVICE_API}/{device_id}/status",
            route_template="/devices/{device_id}/status",
        )

    async def async_get_status(self, device_id: str, endpoint: str) -> dict[str, Any]:
        """Fetch a profile-specific status payload."""
        return await self._async_request_json(
            "GET",
            f"{DEVICE_API}/{device_id}/status/{endpoint}",
            route_template="/devices/{device_id}/status/{status}",
        )

    async def async_get_device_settings(self, device_id: str) -> dict[str, Any]:
        """Fetch writable device settings."""
        return await self._async_request_json(
            "GET",
            f"{DEVICE_API}/{device_id}/settings",
            route_template="/devices/{device_id}/settings",
        )

    async def async_patch_device_settings(
        self,
        device_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Write device settings."""
        try:
            await self._async_request_json(
                "PATCH",
                f"{DEVICE_API}/{device_id}/settings",
                payload=payload,
                route_template="/devices/{device_id}/settings",
                user_initiated=True,
            )
        except AprilaireCloudRateLimitError as err:
            if err.retry_after <= SHORT_WRITE_RETRY_THRESHOLD_SECONDS:
                LOGGER.debug(
                    "Short rate limit on write (%.1fs), auto-retrying", err.retry_after
                )
                await asyncio.sleep(err.retry_after)
                await self._async_request_json(
                    "PATCH",
                    f"{DEVICE_API}/{device_id}/settings",
                    payload=payload,
                    route_template="/devices/{device_id}/settings",
                    user_initiated=True,
                )
                return
            raise

    async def _async_request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        route_template: str = "/redacted",
        allow_retry: bool = True,
        user_initiated: bool = False,
    ) -> dict[str, Any]:
        """Issue a REST request and decode the JSON response."""
        if self._rate_limited_until is not None:
            remaining = (self._rate_limited_until - datetime.now(tz=UTC)).total_seconds()
            if remaining > 0:
                LOGGER.debug("Rate limited, %.1fs remaining", remaining)
                raise AprilaireCloudRateLimitError(remaining)
            self._rate_limited_until = None

        token = await self.async_get_id_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT),
            ) as response:
                return await self._async_handle_response(
                    response,
                    method=method,
                    url=url,
                    route_template=route_template,
                    payload=payload,
                    allow_retry=allow_retry,
                    user_initiated=user_initiated,
                )
        except AprilaireCloudApiError:
            raise
        except (ClientError, TimeoutError, gaierror) as err:
            raise AprilaireCloudCommunicationError(
                context=ApiErrorContext(method=method, route=route_template)
            ) from err

    async def _async_handle_response(
        self,
        response: ClientResponse,
        *,
        method: str,
        url: str,
        route_template: str = "/redacted",
        payload: dict[str, Any] | None,
        allow_retry: bool,
        user_initiated: bool,
    ) -> dict[str, Any]:
        """Convert a REST response into a Python object."""
        if response.status == 401:
            if not allow_retry:
                raise AprilaireCloudAuthenticationTransientError(
                    "http_session_rejected",
                    operation=self._auth.metadata.last_operation
                    or AuthOperation.REFRESH,
                )
            await self.async_get_id_token(force_refresh=True)
            return await self._async_request_json(
                method,
                url,
                payload=payload,
                route_template=route_template,
                allow_retry=False,
                user_initiated=user_initiated,
            )

        if response.status == 429:
            retry_after = _sanitize_retry_after(response.headers.get("Retry-After"))
            self._rate_limited_until = datetime.now(tz=UTC) + timedelta(seconds=retry_after)
            LOGGER.debug("Rate limit hit, retry after %.1fs", retry_after)
            raise AprilaireCloudRateLimitError(retry_after)

        if response.status >= 400:
            text = await response.text()
            context = ApiErrorContext(
                status=response.status,
                method=method,
                route=route_template,
                vendor_code=_sanitized_vendor_code(text),
            )
            LOGGER.warning(
                "%s %s failed with HTTP %d%s",
                method,
                route_template,
                response.status,
                f" ({context.vendor_code})" if context.vendor_code else "",
            )
            raise AprilaireCloudApiError(context=context)

        text = await response.text()
        if not text:
            return {}

        try:
            result: dict[str, Any] = json.loads(text)
            if not isinstance(result, dict):
                raise TypeError
            return result
        except (json.JSONDecodeError, TypeError) as err:
            raise AprilaireCloudApiError(
                "AprilAire API returned invalid JSON",
                context=ApiErrorContext(method=method, route=route_template),
            ) from err
