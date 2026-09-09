"""Deterministic tests for signer-owned credential refresh."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

import pytest

from omnigent.inner.model_auth import ProviderAuthRequired
from omnigent.inner.model_credential import (
    CredentialLifecycle,
    _authoritative_expiry,
)


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ParkedSleep:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.started.set()
        await asyncio.Future()


def _jwt(**claims: object) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(
        b"="
    )
    return f"unsigned.{payload.decode()}.not-verified"


def _raw_jwt(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    return f"unsigned.{encoded.decode()}.not-verified"


def _auth_required() -> ProviderAuthRequired:
    return ProviderAuthRequired("Provider authentication required. Retry.")


def _lifecycle(
    helper: Callable[[], Awaitable[str]],
    clock: _Clock,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> CredentialLifecycle:
    return CredentialLifecycle(
        helper=helper,
        auth_required=_auth_required,
        max_cache_age_s=60.0,
        refresh_skew_s=10.0,
        min_refresh_interval_s=2.0,
        backoff_base_s=4.0,
        backoff_max_s=16.0,
        clock=clock,
        sleep=sleep or _ParkedSleep(),
    )


def test_opaque_token_uses_binding_maximum_cache_age() -> None:
    assert _authoritative_expiry("opaque-token", now=1_000.0, max_cache_age_s=60.0) == 1_060.0


@pytest.mark.parametrize(
    "token",
    [
        _jwt(exp="1060"),
        _jwt(exp=True),
        _jwt(exp=float("nan")),
        _jwt(exp=1_000.0),
        _jwt(nbf="900"),
        _jwt(nbf=True),
        _jwt(nbf=float("inf")),
        _jwt(nbf=1_001.0, exp=1_060.0),
        _jwt(nbf=1_000.0, exp=999.0),
        _raw_jwt(b'{"exp":1060,"exp":1070}'),
        "unsigned.not-base64.not-verified",
        "unsigned.\N{SNOWMAN}.not-verified",
    ],
)
def test_invalid_or_unusable_jwt_claims_are_rejected(token: str) -> None:
    with pytest.raises(ValueError, match="credential is invalid"):
        _authoritative_expiry(token, now=1_000.0, max_cache_age_s=60.0)


def test_known_expiry_may_shorten_but_never_expand_binding_maximum() -> None:
    assert _authoritative_expiry(_jwt(exp=1_025), now=1_000.0, max_cache_age_s=60.0) == 1_025.0
    assert _authoritative_expiry(_jwt(exp=9_999), now=1_000.0, max_cache_age_s=60.0) == 1_060.0


async def test_preflight_schedules_refresh_before_authoritative_expiry() -> None:
    clock = _Clock()
    sleep = _ParkedSleep()
    calls = 0

    async def helper() -> str:
        nonlocal calls
        calls += 1
        return "opaque-token"

    lifecycle = _lifecycle(helper, clock, sleep=sleep)
    await lifecycle.start()
    await sleep.started.wait()

    assert calls == 1
    assert sleep.delays == [50.0]
    await lifecycle.close()
    assert lifecycle.refresh_task is None


async def test_concurrent_requests_coalesce_one_refresh() -> None:
    clock = _Clock()
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def helper() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            refresh_started.set()
            await release_refresh.wait()
        return f"opaque-token-{calls}"

    lifecycle = _lifecycle(helper, clock)
    await lifecycle.start()
    clock.advance(51)
    requests = [asyncio.create_task(lifecycle.token_for_request()) for _ in range(3)]
    await refresh_started.wait()
    assert calls == 2

    release_refresh.set()
    assert await asyncio.gather(*requests) == ["opaque-token-2"] * 3
    assert calls == 2
    await lifecycle.close()


async def test_refresh_backoff_and_minimum_interval_keep_unexpired_token() -> None:
    clock = _Clock()
    outcomes: list[str | Exception] = [
        "opaque-old",
        RuntimeError("SECRET helper stderr"),
        "opaque-new",
    ]

    async def helper() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    lifecycle = _lifecycle(helper, clock)
    await lifecycle.start()
    clock.advance(51)
    assert await lifecycle.token_for_request() == "opaque-old"
    assert len(outcomes) == 1

    clock.advance(3)
    assert await lifecycle.token_for_request() == "opaque-old"
    assert len(outcomes) == 1

    clock.advance(1)
    assert await lifecycle.token_for_request() == "opaque-new"
    assert outcomes == []
    await lifecycle.close()


async def test_refresh_backoff_is_exponential_and_bounded() -> None:
    clock = _Clock()
    calls = 0

    async def helper() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "opaque-old"
        raise RuntimeError("unavailable")

    lifecycle = _lifecycle(helper, clock)
    await lifecycle.start()
    observed_delays: list[float] = []
    for _ in range(5):
        with pytest.raises(ProviderAuthRequired):
            await lifecycle._refresh(bypass_throttle=True)
        delay = lifecycle._next_attempt_at - clock()
        observed_delays.append(delay)
        clock.advance(delay)

    assert observed_delays == [4.0, 8.0, 16.0, 16.0, 16.0]
    await lifecycle.close()


async def test_unauthorized_invalidates_for_next_request_without_replaying() -> None:
    clock = _Clock()
    calls = 0

    async def helper() -> str:
        nonlocal calls
        calls += 1
        return f"opaque-token-{calls}"

    lifecycle = _lifecycle(helper, clock)
    await lifecycle.start()
    lifecycle.invalidate_after_unauthorized()

    assert calls == 1
    assert await lifecycle.token_for_request() == "opaque-token-2"
    assert calls == 2

    lifecycle.invalidate_after_unauthorized()
    with pytest.raises(ProviderAuthRequired):
        await lifecycle.token_for_request()
    assert calls == 2
    clock.advance(2)
    assert await lifecycle.token_for_request() == "opaque-token-3"
    assert calls == 3
    await lifecycle.close()


async def test_expired_token_fails_closed_with_secretless_stable_error() -> None:
    clock = _Clock()
    calls = 0

    async def helper() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _jwt(exp=1_005, private="SECRET_TOKEN_METADATA")
        raise RuntimeError("SECRET_HELPER_STDOUT SECRET_HELPER_STDERR")

    lifecycle = _lifecycle(helper, clock)
    await lifecycle.start()
    clock.advance(6)

    with pytest.raises(ProviderAuthRequired) as raised:
        await lifecycle.token_for_request()

    assert str(raised.value) == "Provider authentication required. Retry."
    assert "SECRET" not in str(raised.value)
    await lifecycle.close()


async def test_close_cancels_refresh_and_helper_lifecycle() -> None:
    clock = _Clock()
    helper_cancelled = asyncio.Event()
    calls = 0

    async def helper() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _jwt(exp=1_020)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            helper_cancelled.set()
            raise

    async def immediate_sleep(delay: float) -> None:
        clock.advance(delay)

    lifecycle = _lifecycle(helper, clock, sleep=immediate_sleep)
    await lifecycle.start()
    while calls < 2:
        await asyncio.sleep(0)
    await lifecycle.close()

    assert helper_cancelled.is_set()
    assert lifecycle.refresh_task is None
