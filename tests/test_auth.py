"""Authentication lifecycle tests for AprilAire Cloud."""

from __future__ import annotations

import asyncio
import json
import time
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

import custom_components.aprilaire_cloud.vendor.auth as auth_module
from custom_components.aprilaire_cloud.vendor.auth import (
    AprilaireCloudAuthenticationProtocolError,
    AprilaireCloudAuthenticationTransientError,
    AprilaireCloudInvalidCredentialsError,
    AuthFailureKind,
    AuthOperation,
    CognitoAuthProvider,
    _classify_exception,
    _sync_authenticate,
    _sync_refresh,
    _validate_tokens,
)

USERNAME = "user@example.com"
PASSWORD = "not-a-real-password"


class Clock:
    """Mutable UTC clock."""

    def __init__(self) -> None:
        """Initialize a fixed time."""
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Return the current test time."""
        return self.value

    def advance(self, **kwargs: float) -> None:
        """Advance the test clock."""
        self.value += timedelta(**kwargs)


def _jwt(expires_at: datetime, marker: str) -> str:
    """Build a synthetic unsigned JWT-shaped token."""

    def _part(value: dict[str, object]) -> str:
        encoded = urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode()
        return encoded.rstrip("=")

    return f"{_part({'alg': 'none'})}.{_part({'exp': expires_at.timestamp(), 'm': marker})}.sig"


def _tokens(clock: Clock, marker: str, *, lifetime: timedelta = timedelta(hours=1)):
    """Return a complete synthetic Cognito token bundle."""
    return {
        "id_token": _jwt(clock() + lifetime, f"id-{marker}"),
        "access_token": f"access-{marker}",
        "refresh_token": f"refresh-{marker}",
    }


def _client_error(code: str) -> ClientError:
    """Build a botocore error with a synthetic private message."""
    return ClientError(
        {"Error": {"Code": code, "Message": "private source error"}},
        "InitiateAuth",
    )


async def test_first_full_authentication_succeeds(monkeypatch) -> None:
    """The first operation performs a full login and records metadata."""
    clock = Clock()
    token_bundle = _tokens(clock, "first")
    monkeypatch.setattr(auth_module, "_sync_authenticate", lambda *_: token_bundle)
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)

    await provider.async_authenticate()

    assert await provider.async_get_id_token() == token_bundle["id_token"]
    assert provider.metadata.last_full_login_at == clock()
    assert provider.metadata.last_operation is AuthOperation.FULL_LOGIN
    assert provider.metadata.last_outcome == "success"
    diagnostics = provider.metadata.as_diagnostics()
    assert diagnostics["last_full_login_at"] == clock().isoformat()
    assert token_bundle["id_token"] not in json.dumps(diagnostics)
    assert token_bundle["refresh_token"] not in json.dumps(diagnostics)


async def test_valid_unexpired_id_token_is_reused(monkeypatch) -> None:
    """Unexpired tokens do not invoke Cognito again."""
    clock = Clock()
    calls = 0

    def _login(*_):
        nonlocal calls
        calls += 1
        return _tokens(clock, "reuse")

    monkeypatch.setattr(auth_module, "_sync_authenticate", _login)
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)

    first = await provider.async_get_id_token()
    second = await provider.async_get_id_token()

    assert first == second
    assert calls == 1


async def test_normal_refresh_succeeds(monkeypatch) -> None:
    """An expiring ID token renews through the refresh token."""
    clock = Clock()
    monkeypatch.setattr(
        auth_module, "_sync_authenticate", lambda *_: _tokens(clock, "initial")
    )
    monkeypatch.setattr(auth_module, "_sync_refresh", lambda *_: _tokens(clock, "refreshed"))
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)
    await provider.async_authenticate()

    refreshed = await provider.async_get_id_token(force_refresh=True)

    assert refreshed == _tokens(clock, "refreshed")["id_token"]
    assert provider.metadata.last_refresh_at == clock()
    assert provider.metadata.last_operation is AuthOperation.REFRESH


async def test_rejected_refresh_falls_back_to_successful_full_login(monkeypatch) -> None:
    """Normal refresh-token expiry is recovered without reauthentication."""
    clock = Clock()
    login_count = 0

    def _login(*_):
        nonlocal login_count
        login_count += 1
        return _tokens(clock, f"full-{login_count}")

    monkeypatch.setattr(auth_module, "_sync_authenticate", _login)
    monkeypatch.setattr(
        auth_module,
        "_sync_refresh",
        lambda *_: (_ for _ in ()).throw(_client_error("NotAuthorizedException")),
    )
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)
    await provider.async_authenticate()

    token = await provider.async_get_id_token(force_refresh=True)

    assert token == _tokens(clock, "full-2")["id_token"]
    assert login_count == 2
    assert provider.metadata.last_outcome == "success"


async def test_transient_refresh_failure_can_recover_with_full_login(monkeypatch) -> None:
    """A refresh outage may recover through a successful full login."""
    clock = Clock()
    login_count = 0

    def _login(*_):
        nonlocal login_count
        login_count += 1
        return _tokens(clock, f"login-{login_count}")

    monkeypatch.setattr(auth_module, "_sync_authenticate", _login)
    monkeypatch.setattr(
        auth_module,
        "_sync_refresh",
        lambda *_: (_ for _ in ()).throw(_client_error("ServiceUnavailableException")),
    )
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)
    await provider.async_authenticate()

    assert await provider.async_get_id_token(force_refresh=True) == _tokens(
        clock, "login-2"
    )["id_token"]
    assert login_count == 2


@pytest.mark.parametrize(
    "code",
    ["ServiceUnavailableException", "TooManyRequestsException", "LimitExceededException"],
)
async def test_transient_full_login_failures_are_retryable(monkeypatch, code: str) -> None:
    """Cognito outages and throttling must never become invalid credentials."""
    monkeypatch.setattr(
        auth_module,
        "_sync_authenticate",
        lambda *_: (_ for _ in ()).throw(_client_error(code)),
    )
    provider = CognitoAuthProvider(USERNAME, PASSWORD)

    with pytest.raises(AprilaireCloudAuthenticationTransientError) as err:
        await provider.async_authenticate()

    assert err.value.code == code
    assert provider.metadata.last_outcome == "transient_error"


async def test_definite_full_login_rejection_requires_reauth(monkeypatch) -> None:
    """Only a full-login account rejection is an invalid-credential result."""
    monkeypatch.setattr(
        auth_module,
        "_sync_authenticate",
        lambda *_: (_ for _ in ()).throw(_client_error("NotAuthorizedException")),
    )
    provider = CognitoAuthProvider(USERNAME, PASSWORD)

    with pytest.raises(AprilaireCloudInvalidCredentialsError):
        await provider.async_authenticate()

    assert provider.metadata.last_outcome == "invalid_credentials"


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"id_token": "broken", "access_token": "a", "refresh_token": "r"},
        {"id_token": "a.b.c", "access_token": "a", "refresh_token": "r"},
    ],
)
async def test_malformed_token_output_is_protocol_failure(monkeypatch, malformed) -> None:
    """Malformed Cognito output is retryable protocol state, not reauth."""
    monkeypatch.setattr(auth_module, "_sync_authenticate", lambda *_: malformed)
    provider = CognitoAuthProvider(USERNAME, PASSWORD)

    with pytest.raises(AprilaireCloudAuthenticationProtocolError):
        await provider.async_authenticate()

    assert provider.metadata.last_outcome == "protocol_error"


async def test_proactive_full_login_resets_age(monkeypatch) -> None:
    """A renewal around day 25 performs a fresh full login."""
    clock = Clock()
    login_count = 0

    def _login(*_):
        nonlocal login_count
        login_count += 1
        return _tokens(clock, f"proactive-{login_count}", lifetime=timedelta(days=60))

    monkeypatch.setattr(auth_module, "_sync_authenticate", _login)
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)
    await provider.async_authenticate()
    clock.advance(days=27)

    await provider.async_get_id_token(force_refresh=True)

    assert login_count == 2
    assert provider.metadata.last_full_login_at == clock()
    assert provider.metadata.last_operation is AuthOperation.FULL_LOGIN


async def test_failed_proactive_login_falls_back_to_refresh(monkeypatch) -> None:
    """A proactive outage must not discard a usable refresh token."""
    clock = Clock()
    login_count = 0

    def _login(*_):
        nonlocal login_count
        login_count += 1
        if login_count == 2:
            raise _client_error("ServiceUnavailableException")
        return _tokens(clock, "initial", lifetime=timedelta(days=60))

    monkeypatch.setattr(auth_module, "_sync_authenticate", _login)
    monkeypatch.setattr(auth_module, "_sync_refresh", lambda *_: _tokens(clock, "fallback"))
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)
    await provider.async_authenticate()
    clock.advance(days=27)

    token = await provider.async_get_id_token(force_refresh=True)

    assert token == _tokens(clock, "fallback")["id_token"]
    assert provider.metadata.last_operation is AuthOperation.REFRESH
    assert provider.metadata.last_outcome == "success"


async def test_concurrent_callers_share_one_authentication(monkeypatch) -> None:
    """The authentication lock makes renewal single-flight."""
    clock = Clock()
    calls = 0
    expected = _tokens(clock, "concurrent")

    def _login(*_):
        nonlocal calls
        calls += 1
        time.sleep(0.01)
        return expected

    monkeypatch.setattr(auth_module, "_sync_authenticate", _login)
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)

    results = await asyncio.gather(*(provider.async_get_id_token() for _ in range(12)))

    assert results == [expected["id_token"]] * 12
    assert calls == 1


async def test_auth_logs_never_include_credentials_or_tokens(monkeypatch, caplog) -> None:
    """Lifecycle logging must contain no user, password, token, or raw source message."""
    private_token_marker = "private-token-material"
    source_message = "private-source-message"
    def _fail(*_):
        raise ClientError(
            {
                "Error": {
                    "Code": "ServiceUnavailableException",
                    "Message": source_message,
                }
            },
            "InitiateAuth",
        )

    monkeypatch.setattr(auth_module, "_sync_authenticate", _fail)
    monkeypatch.setattr(auth_module, "_sync_refresh", _fail)
    provider = CognitoAuthProvider(USERNAME, PASSWORD)
    provider._refresh_token = private_token_marker

    with pytest.raises(AprilaireCloudAuthenticationTransientError):
        await provider.async_get_id_token(force_refresh=True)

    assert USERNAME not in caplog.text
    assert PASSWORD not in caplog.text
    assert private_token_marker not in caplog.text
    assert source_message not in caplog.text


def test_sync_pycognito_boundaries_extract_only_token_fields(monkeypatch) -> None:
    """Synchronous adapters return the narrow token bundle expected by async code."""

    class FakeCognito:
        id_token = "id"
        access_token = "access"
        refresh_token = "refresh"

        def authenticate(self, *, password: str) -> None:
            assert password == PASSWORD

        def renew_access_token(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "Cognito", lambda **kwargs: FakeCognito())

    assert _sync_authenticate(USERNAME, PASSWORD) == {
        "id_token": "id",
        "access_token": "access",
        "refresh_token": "refresh",
    }
    assert _sync_refresh(USERNAME, "old-refresh") == {
        "id_token": "id",
        "access_token": "access",
        "refresh_token": "refresh",
    }


def test_auth_classifier_and_bundle_boundary_cover_unknown_failures() -> None:
    """Unknown local failures are protocol errors, never invalid credentials."""
    with pytest.raises(
        AprilaireCloudAuthenticationProtocolError,
        match="malformed_token_bundle",
    ):
        _validate_tokens("not-a-dict", operation=AuthOperation.FULL_LOGIN)

    transient = _classify_exception(
        TimeoutError(),
        operation=AuthOperation.FULL_LOGIN,
    )
    protocol = _classify_exception(
        RuntimeError(),
        operation=AuthOperation.FULL_LOGIN,
    )
    unknown_client = _classify_exception(
        _client_error("UnexpectedException"),
        operation=AuthOperation.FULL_LOGIN,
    )

    assert transient.kind is AuthFailureKind.TRANSIENT
    assert protocol.kind is AuthFailureKind.PROTOCOL
    assert unknown_client.kind is AuthFailureKind.PROTOCOL


async def test_token_expiry_query_before_and_after_login(monkeypatch) -> None:
    """Expiry checks are conservative before auth and timestamp-based afterward."""
    clock = Clock()
    monkeypatch.setattr(
        auth_module,
        "_sync_authenticate",
        lambda *_: _tokens(clock, "expiry"),
    )
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)

    assert await provider.async_token_expires_within(60) is True
    await provider.async_authenticate()
    assert await provider.async_token_expires_within(60) is False
    clock.advance(minutes=59, seconds=30)
    assert await provider.async_token_expires_within(60) is True


async def test_malformed_refresh_output_is_protocol_failure(monkeypatch) -> None:
    """A malformed refresh response is not treated as bad credentials."""
    clock = Clock()
    monkeypatch.setattr(
        auth_module,
        "_sync_authenticate",
        lambda *_: _tokens(clock, "initial"),
    )
    monkeypatch.setattr(auth_module, "_sync_refresh", lambda *_: {})
    provider = CognitoAuthProvider(USERNAME, PASSWORD, now=clock)
    await provider.async_authenticate()

    with pytest.raises(AprilaireCloudAuthenticationProtocolError):
        await provider._async_refresh_locked()

    assert provider.metadata.last_operation is AuthOperation.REFRESH
    assert provider.metadata.last_outcome == "protocol_error"


async def test_unknown_full_login_exception_is_protocol_failure(monkeypatch) -> None:
    """An unexpected executor failure remains a sanitized protocol error."""
    monkeypatch.setattr(
        auth_module,
        "_sync_authenticate",
        lambda *_: (_ for _ in ()).throw(RuntimeError("private")),
    )
    provider = CognitoAuthProvider(USERNAME, PASSWORD)

    with pytest.raises(AprilaireCloudAuthenticationProtocolError) as err:
        await provider.async_authenticate()

    assert err.value.code == "RuntimeError"
