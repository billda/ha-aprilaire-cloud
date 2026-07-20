"""Cognito authentication lifecycle for AprilAire Cloud."""

from __future__ import annotations

import asyncio
import hashlib
import json
from base64 import urlsafe_b64decode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from socket import gaierror
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pycognito import Cognito
from pycognito.exceptions import (
    ForceChangePasswordException,
    MFAChallengeException,
    TokenVerificationException,
)

from ..const import (
    AUTH_REFRESH_MARGIN_SECONDS,
    COGNITO_CLIENT_ID,
    COGNITO_REGION,
    COGNITO_USER_POOL_ID,
    LOGGER,
)

PROACTIVE_FULL_LOGIN_AGE = timedelta(days=25)
PROACTIVE_FULL_LOGIN_JITTER_SECONDS = 24 * 60 * 60

_DEFINITE_ACCOUNT_CODES = {
    "NotAuthorizedException",
    "PasswordResetRequiredException",
    "UserDisabledException",
    "UserNotConfirmedException",
    "UserNotFoundException",
}
_TRANSIENT_SERVICE_CODES = {
    "InternalErrorException",
    "LimitExceededException",
    "RequestLimitExceeded",
    "ServiceUnavailableException",
    "ThrottlingException",
    "TooManyRequestsException",
}


class AuthOperation(StrEnum):
    """Authentication operation names safe for diagnostics."""

    FULL_LOGIN = "full_login"
    REFRESH = "refresh"


class AuthFailureKind(StrEnum):
    """Internal classification of a Cognito failure."""

    INVALID_CREDENTIALS = "invalid_credentials"
    REFRESH_REJECTED = "refresh_rejected"
    TRANSIENT = "transient"
    PROTOCOL = "protocol"


class AprilaireCloudAuthenticationError(Exception):
    """Base class for sanitized authentication failures."""

    def __init__(self, code: str, operation: AuthOperation) -> None:
        """Initialize an authentication error without source exception text."""
        self.code = code
        self.operation = operation
        super().__init__(f"AprilAire authentication failed ({operation.value}, {code})")


class AprilaireCloudInvalidCredentialsError(AprilaireCloudAuthenticationError):
    """A full login proved that user action is required."""


class AprilaireCloudAuthenticationTransientError(AprilaireCloudAuthenticationError):
    """Authentication infrastructure is temporarily unavailable."""


class AprilaireCloudAuthenticationProtocolError(AprilaireCloudAuthenticationError):
    """Authentication returned an unknown or malformed response."""


@dataclass(frozen=True, slots=True)
class AuthLifecycleMetadata:
    """Value-free authentication lifecycle metadata."""

    last_full_login_at: datetime | None
    last_refresh_at: datetime | None
    token_expires_at: datetime | None
    last_operation: AuthOperation | None
    last_outcome: str | None
    last_error_code: str | None

    def as_diagnostics(self) -> dict[str, str | None]:
        """Return JSON-serializable, token-free diagnostics."""
        return {
            "last_full_login_at": _isoformat(self.last_full_login_at),
            "last_refresh_at": _isoformat(self.last_refresh_at),
            "token_expires_at": _isoformat(self.token_expires_at),
            "last_operation": self.last_operation.value if self.last_operation else None,
            "last_outcome": self.last_outcome,
            "last_error_code": self.last_error_code,
        }


@dataclass(frozen=True, slots=True)
class _ClassifiedFailure:
    """Internal sanitized failure classification."""

    kind: AuthFailureKind
    code: str


def _isoformat(value: datetime | None) -> str | None:
    """Serialize a timestamp in UTC."""
    return value.isoformat() if value is not None else None


def _sync_authenticate(username: str, password: str) -> dict[str, str]:
    """Perform pycognito's synchronous SRP login."""
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
    """Perform pycognito's synchronous refresh flow."""
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
    """Decode an unverified JWT expiry solely for local renewal scheduling."""
    try:
        segments = token.split(".")
        if len(segments) != 3:
            raise ValueError
        payload = segments[1] + "=" * (-len(segments[1]) % 4)
        data = json.loads(urlsafe_b64decode(payload.encode()))
        expiry = data["exp"]
        if isinstance(expiry, bool) or not isinstance(expiry, int | float):
            raise ValueError
        return datetime.fromtimestamp(expiry, tz=UTC)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise AprilaireCloudAuthenticationProtocolError(
            "malformed_id_token", AuthOperation.FULL_LOGIN
        ) from err


def _validate_tokens(
    value: Any,
    *,
    operation: AuthOperation,
) -> tuple[dict[str, str], datetime]:
    """Validate token bundle structure without retaining or logging claims."""
    if not isinstance(value, dict):
        raise AprilaireCloudAuthenticationProtocolError("malformed_token_bundle", operation)
    required = ("id_token", "access_token", "refresh_token")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise AprilaireCloudAuthenticationProtocolError("missing_token_field", operation)
    tokens = {key: value[key] for key in required}
    try:
        expiry = _decode_expiry(tokens["id_token"])
    except AprilaireCloudAuthenticationProtocolError as err:
        raise AprilaireCloudAuthenticationProtocolError(err.code, operation) from err
    return tokens, expiry


def _client_error_code(error: ClientError) -> str:
    """Extract only botocore's bounded machine error code."""
    code = error.response.get("Error", {}).get("Code")
    return code if isinstance(code, str) and 0 < len(code) <= 64 else "client_error"


def _classify_exception(
    error: Exception,
    *,
    operation: AuthOperation,
) -> _ClassifiedFailure:
    """Classify pycognito/botocore failures without using their messages."""
    if isinstance(error, (ForceChangePasswordException, MFAChallengeException)):
        return _ClassifiedFailure(AuthFailureKind.INVALID_CREDENTIALS, "challenge_required")
    if isinstance(error, ClientError):
        code = _client_error_code(error)
        if code == "NotAuthorizedException" and operation is AuthOperation.REFRESH:
            return _ClassifiedFailure(AuthFailureKind.REFRESH_REJECTED, "refresh_rejected")
        if code in _DEFINITE_ACCOUNT_CODES and operation is AuthOperation.FULL_LOGIN:
            return _ClassifiedFailure(AuthFailureKind.INVALID_CREDENTIALS, code)
        if code in _TRANSIENT_SERVICE_CODES:
            return _ClassifiedFailure(AuthFailureKind.TRANSIENT, code)
        return _ClassifiedFailure(AuthFailureKind.PROTOCOL, code)
    if isinstance(error, TokenVerificationException):
        return _ClassifiedFailure(AuthFailureKind.TRANSIENT, "token_verification_unavailable")
    if isinstance(error, (BotoCoreError, TimeoutError, gaierror, ConnectionError)):
        return _ClassifiedFailure(AuthFailureKind.TRANSIENT, type(error).__name__)
    if type(error).__module__.startswith(("requests", "urllib3")):
        return _ClassifiedFailure(AuthFailureKind.TRANSIENT, type(error).__name__)
    return _ClassifiedFailure(AuthFailureKind.PROTOCOL, type(error).__name__)


class CognitoAuthProvider:
    """Own Cognito tokens and serialize every authentication operation."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize an in-memory authentication provider."""
        self._username = username
        self._password = password
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._lock = asyncio.Lock()
        self._id_token: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._last_full_login_at: datetime | None = None
        self._last_refresh_at: datetime | None = None
        self._last_operation: AuthOperation | None = None
        self._last_outcome: str | None = None
        self._last_error_code: str | None = None
        digest = hashlib.sha256(username.encode()).digest()
        self._proactive_jitter = timedelta(
            seconds=int.from_bytes(digest[:4]) % PROACTIVE_FULL_LOGIN_JITTER_SECONDS
        )

    @property
    def metadata(self) -> AuthLifecycleMetadata:
        """Return token-free lifecycle metadata."""
        return AuthLifecycleMetadata(
            last_full_login_at=self._last_full_login_at,
            last_refresh_at=self._last_refresh_at,
            token_expires_at=self._token_expires_at,
            last_operation=self._last_operation,
            last_outcome=self._last_outcome,
            last_error_code=self._last_error_code,
        )

    async def async_authenticate(self) -> None:
        """Perform an explicit full login."""
        async with self._lock:
            await self._async_full_login_locked()

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid ID token, renewing it through a single-flight path."""
        async with self._lock:
            now = self._now()
            if (
                not force_refresh
                and self._id_token is not None
                and self._token_expires_at is not None
                and now
                < self._token_expires_at - timedelta(seconds=AUTH_REFRESH_MARGIN_SECONDS)
            ):
                return self._id_token

            proactive = self._proactive_full_login_due(now)
            if proactive:
                try:
                    return await self._async_full_login_locked()
                except AprilaireCloudAuthenticationTransientError:
                    LOGGER.debug("Proactive full login unavailable; trying token refresh")
                except AprilaireCloudAuthenticationProtocolError:
                    LOGGER.debug("Proactive full login was inconclusive; trying token refresh")

            if self._refresh_token is not None:
                try:
                    return await self._async_refresh_locked()
                except AprilaireCloudAuthenticationError:
                    LOGGER.debug("Token refresh unavailable; trying full login")

            return await self._async_full_login_locked()

    async def async_token_expires_within(self, seconds: int) -> bool:
        """Return whether the current ID token is near expiry."""
        if self._token_expires_at is None:
            return True
        return self._now() >= self._token_expires_at - timedelta(seconds=seconds)

    def _proactive_full_login_due(self, now: datetime) -> bool:
        """Return whether full-login age should be reset on this renewal."""
        return (
            self._last_full_login_at is not None
            and now
            >= self._last_full_login_at + PROACTIVE_FULL_LOGIN_AGE + self._proactive_jitter
        )

    async def _async_full_login_locked(self) -> str:
        """Perform a full login while the caller holds the provider lock."""
        operation = AuthOperation.FULL_LOGIN
        try:
            value = await asyncio.get_running_loop().run_in_executor(
                None,
                _sync_authenticate,
                self._username,
                self._password,
            )
            tokens, expiry = _validate_tokens(value, operation=operation)
        except AprilaireCloudAuthenticationProtocolError as err:
            self._record_failure(operation, err.code, "protocol_error")
            raise
        except Exception as err:
            self._raise_classified(err, operation)

        now = self._now()
        self._store_tokens(tokens, expiry)
        self._last_full_login_at = now
        self._record_success(operation)
        return tokens["id_token"]

    async def _async_refresh_locked(self) -> str:
        """Refresh tokens while the caller holds the provider lock."""
        operation = AuthOperation.REFRESH
        assert self._refresh_token is not None
        try:
            value = await asyncio.get_running_loop().run_in_executor(
                None,
                _sync_refresh,
                self._username,
                self._refresh_token,
            )
            tokens, expiry = _validate_tokens(value, operation=operation)
        except AprilaireCloudAuthenticationProtocolError as err:
            self._record_failure(operation, err.code, "protocol_error")
            raise
        except Exception as err:
            self._raise_classified(err, operation)

        self._store_tokens(tokens, expiry)
        self._last_refresh_at = self._now()
        self._record_success(operation)
        return tokens["id_token"]

    def _raise_classified(self, error: Exception, operation: AuthOperation) -> None:
        """Record and raise a typed sanitized authentication failure."""
        failure = _classify_exception(error, operation=operation)
        if failure.kind is AuthFailureKind.INVALID_CREDENTIALS:
            self._record_failure(operation, failure.code, "invalid_credentials")
            raise AprilaireCloudInvalidCredentialsError(failure.code, operation) from error
        if failure.kind in {AuthFailureKind.TRANSIENT, AuthFailureKind.REFRESH_REJECTED}:
            self._record_failure(operation, failure.code, "transient_error")
            raise AprilaireCloudAuthenticationTransientError(
                failure.code, operation
            ) from error
        self._record_failure(operation, failure.code, "protocol_error")
        raise AprilaireCloudAuthenticationProtocolError(failure.code, operation) from error

    def _store_tokens(self, tokens: dict[str, str], expiry: datetime) -> None:
        """Store validated tokens only in memory."""
        self._id_token = tokens["id_token"]
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._token_expires_at = expiry

    def _record_success(self, operation: AuthOperation) -> None:
        """Record a sanitized successful lifecycle outcome."""
        self._last_operation = operation
        self._last_outcome = "success"
        self._last_error_code = None

    def _record_failure(self, operation: AuthOperation, code: str, outcome: str) -> None:
        """Record a sanitized failed lifecycle outcome."""
        self._last_operation = operation
        self._last_outcome = outcome
        self._last_error_code = code
