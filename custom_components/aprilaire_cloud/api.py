"""Async API client for AprilAire Cloud."""

from __future__ import annotations

import asyncio
import json
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from socket import gaierror
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from pycognito import Cognito

from .const import (
    ACCOUNT_API,
    AUTH_REFRESH_MARGIN_SECONDS,
    COGNITO_CLIENT_ID,
    COGNITO_REGION,
    COGNITO_USER_POOL_ID,
    DEFAULT_RATE_LIMIT_RETRY_SECONDS,
    DEFAULT_REQUEST_TIMEOUT,
    DEVICE_API,
    LOGGER,
    MAX_RATE_LIMIT_RETRY_SECONDS,
    SHORT_WRITE_RETRY_THRESHOLD_SECONDS,
)


class AprilaireCloudApiError(Exception):
    """Base API error."""


class AprilaireCloudAuthenticationError(AprilaireCloudApiError):
    """Authentication failure."""


class AprilaireCloudCommunicationError(AprilaireCloudApiError):
    """Network or transport failure."""


@dataclass(slots=True)
class AprilaireCloudRateLimitError(AprilaireCloudApiError):
    """A request was throttled."""

    retry_after: float


class AprilaireCloudWriteError(AprilaireCloudApiError):
    """A requested write could not be confirmed."""


def _sync_authenticate(username: str, password: str) -> dict[str, str]:
    """Authenticate against Cognito."""
    cognito = Cognito(
        user_pool_id=COGNITO_USER_POOL_ID,
        client_id=COGNITO_CLIENT_ID,
        user_pool_region=COGNITO_REGION,
        username=username,
    )
    cognito.authenticate(password=password)
    return {
        "id_token": cognito.id_token,
        "access_token": cognito.access_token,
        "refresh_token": cognito.refresh_token,
    }


def _sync_refresh(username: str, refresh_token: str) -> dict[str, str]:
    """Refresh an existing Cognito session."""
    cognito = Cognito(
        user_pool_id=COGNITO_USER_POOL_ID,
        client_id=COGNITO_CLIENT_ID,
        user_pool_region=COGNITO_REGION,
        username=username,
        refresh_token=refresh_token,
    )
    cognito.renew_access_token()
    return {
        "id_token": cognito.id_token,
        "access_token": cognito.access_token,
        "refresh_token": cognito.refresh_token or refresh_token,
    }


def _decode_expiry(token: str) -> datetime:
    """Decode the `exp` timestamp from a JWT without verifying it."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    data = json.loads(urlsafe_b64decode(payload.encode()))
    return datetime.fromtimestamp(data["exp"], tz=UTC)


def _sanitize_retry_after(value: str | None) -> float:
    """Clamp a retry delay to a reasonable range."""
    if value is None:
        return DEFAULT_RATE_LIMIT_RETRY_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_RATE_LIMIT_RETRY_SECONDS
    return max(1.0, min(parsed, float(MAX_RATE_LIMIT_RETRY_SECONDS)))


class AprilaireCloudApiClient:
    """AprilAire Cloud REST client."""

    def __init__(
        self,
        username: str,
        password: str,
        session: ClientSession,
    ) -> None:
        """Initialize the client."""
        self._username = username
        self._password = password
        self._session = session
        self._auth_lock = asyncio.Lock()
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: datetime | None = None
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

    async def async_authenticate(self) -> None:
        """Perform a full authentication."""
        try:
            tokens = await asyncio.get_running_loop().run_in_executor(
                None,
                _sync_authenticate,
                self._username,
                self._password,
            )
        except Exception as err:
            raise AprilaireCloudAuthenticationError(str(err)) from err
        self._store_tokens(tokens)

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid ID token."""
        async with self._auth_lock:
            if (
                not force_refresh
                and self._id_token is not None
                and self._token_expires_at is not None
                and datetime.now(tz=UTC)
                < self._token_expires_at - timedelta(seconds=AUTH_REFRESH_MARGIN_SECONDS)
            ):
                return self._id_token

            if self._refresh_token is not None:
                try:
                    tokens = await asyncio.get_running_loop().run_in_executor(
                        None,
                        _sync_refresh,
                        self._username,
                        self._refresh_token,
                    )
                except Exception as err:
                    LOGGER.debug("Refresh token flow failed, falling back to full login: %s", err)
                else:
                    LOGGER.debug("Token refreshed successfully")
                    self._store_tokens(tokens)
                    assert self._id_token is not None
                    return self._id_token

            try:
                await self.async_authenticate()
            except Exception as err:
                raise AprilaireCloudAuthenticationError(str(err)) from err

            if self._id_token is None:
                raise AprilaireCloudAuthenticationError("Authentication did not return an ID token")

            return self._id_token

    async def async_token_expires_within(self, seconds: int) -> bool:
        """Return whether the ID token expires within the given interval."""
        if self._token_expires_at is None:
            return True
        return datetime.now(tz=UTC) >= self._token_expires_at - timedelta(seconds=seconds)

    async def async_get_user(self) -> dict[str, Any]:
        """Fetch the authenticated user profile."""
        return await self._async_request_json("GET", f"{ACCOUNT_API}/user")

    async def async_get_hierarchy(self) -> dict[str, Any]:
        """Fetch the full hierarchy."""
        return await self._async_request_json("GET", f"{DEVICE_API}/hierarchy")

    async def async_get_device_status(self, device_id: str) -> dict[str, Any]:
        """Fetch device status."""
        return await self._async_request_json("GET", f"{DEVICE_API}/{device_id}/status")

    async def async_get_status(self, device_id: str, endpoint: str) -> dict[str, Any]:
        """Fetch a profile-specific status payload."""
        return await self._async_request_json("GET", f"{DEVICE_API}/{device_id}/status/{endpoint}")

    async def async_get_device_settings(self, device_id: str) -> dict[str, Any]:
        """Fetch writable device settings."""
        return await self._async_request_json("GET", f"{DEVICE_API}/{device_id}/settings")

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
                    user_initiated=True,
                )
                return
            raise

    def _store_tokens(self, tokens: dict[str, str]) -> None:
        """Persist freshly-issued tokens in memory."""
        self._id_token = tokens["id_token"]
        self._refresh_token = tokens["refresh_token"]
        self._token_expires_at = _decode_expiry(self._id_token)

    async def _async_request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
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
                    payload=payload,
                    allow_retry=allow_retry,
                    user_initiated=user_initiated,
                )
        except AprilaireCloudApiError:
            raise
        except (ClientError, TimeoutError, gaierror) as err:
            raise AprilaireCloudCommunicationError(str(err)) from err

    async def _async_handle_response(
        self,
        response: ClientResponse,
        *,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        allow_retry: bool,
        user_initiated: bool,
    ) -> dict[str, Any]:
        """Convert a REST response into a Python object."""
        if response.status == 401:
            if not allow_retry:
                raise AprilaireCloudAuthenticationError("Unauthorized")
            await self.async_get_id_token(force_refresh=True)
            return await self._async_request_json(
                method,
                url,
                payload=payload,
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
            LOGGER.warning(
                "%s %s failed with HTTP %d: %s", method, url, response.status, text[:200]
            )
            raise AprilaireCloudApiError(
                f"{method} {url} failed with HTTP {response.status}: {text[:200]}"
            )

        text = await response.text()
        if not text:
            return {}

        try:
            result: dict[str, Any] = json.loads(text)
            return result
        except json.JSONDecodeError as err:
            raise AprilaireCloudApiError(f"{method} {url} returned invalid JSON") from err
