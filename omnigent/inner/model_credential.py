"""Signer-private credential lifetime and refresh management."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .model_auth import ProviderAuthRequired

_BASE64URL_RE = re.compile(rb"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class _Credential:
    token: str
    expires_at: float


def _numeric_date(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("credential is invalid")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("credential is invalid") from None
    if not math.isfinite(result):
        raise ValueError("credential is invalid")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("credential is invalid")
        result[key] = value
    return result


def _authoritative_expiry(token: str, *, now: float, max_cache_age_s: float) -> float:
    """Bound token use without treating decoded JWT claims as verified."""
    maximum = now + max_cache_age_s
    if token.count(".") != 2:
        return maximum

    try:
        payload_segment = token.split(".", 2)[1].encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("credential is invalid") from None
    if not payload_segment or _BASE64URL_RE.fullmatch(payload_segment) is None:
        raise ValueError("credential is invalid")
    try:
        padded = payload_segment + b"=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True),
            object_pairs_hook=_unique_object,
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("credential is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("credential is invalid")

    nbf = _numeric_date(payload["nbf"]) if "nbf" in payload else None
    exp = _numeric_date(payload["exp"]) if "exp" in payload else None
    if nbf is not None and nbf > now:
        raise ValueError("credential is invalid")
    if exp is not None and exp <= now:
        raise ValueError("credential is invalid")
    if nbf is not None and exp is not None and exp <= nbf:
        raise ValueError("credential is invalid")
    return min(maximum, exp) if exp is not None else maximum


class CredentialLifecycle:
    """Own one provider credential cache entirely inside the signer."""

    def __init__(
        self,
        *,
        helper: Callable[[], Awaitable[str]],
        auth_required: Callable[[], ProviderAuthRequired],
        max_cache_age_s: float,
        refresh_skew_s: float,
        min_refresh_interval_s: float,
        backoff_base_s: float,
        backoff_max_s: float,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            max_cache_age_s <= 0
            or refresh_skew_s < 0
            or min_refresh_interval_s <= 0
            or backoff_base_s <= 0
            or backoff_max_s < backoff_base_s
        ):
            raise ValueError("credential lifecycle timing is invalid")
        self._helper = helper
        self._auth_required = auth_required
        self._max_cache_age_s = max_cache_age_s
        self._refresh_skew_s = refresh_skew_s
        self._min_refresh_interval_s = min_refresh_interval_s
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._clock = clock
        self._sleep = sleep
        self._credential: _Credential | None = None
        self._lock = asyncio.Lock()
        self._attempt_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._next_attempt_at = 0.0
        self._next_unauthorized_refresh_at = 0.0
        self._failures = 0
        self._invalidated = False
        self._closed = False

    @property
    def refresh_task(self) -> asyncio.Task[None] | None:
        """Expose only lifecycle state for supervision tests."""
        return self._refresh_task

    async def start(self) -> None:
        """Mint once before readiness, then own background refresh."""
        if self._closed or self._credential is not None:
            raise RuntimeError("credential lifecycle cannot be started")
        await self._refresh(bypass_throttle=True)
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def token_for_request(self) -> str:
        """Return a currently authoritative token or fail closed."""
        if self._closed:
            raise self._auth_required()
        credential = self._credential
        now = self._clock()
        if (
            credential is not None
            and now < credential.expires_at
            and not self._invalidated
            and now < credential.expires_at - self._refresh_skew_s
        ):
            return credential.token

        try:
            await self._refresh()
        except ProviderAuthRequired:
            credential = self._credential
            if credential is None or self._clock() >= credential.expires_at:
                raise
        credential = self._credential
        now = self._clock()
        if credential is None or now >= credential.expires_at or self._invalidated:
            raise self._auth_required()
        return credential.token

    def invalidate_after_unauthorized(self) -> None:
        """Make the next request coalesce one refresh; never replay this request."""
        if self._closed or self._invalidated:
            return
        self._invalidated = True
        now = self._clock()
        if now >= self._next_unauthorized_refresh_at:
            self._next_attempt_at = min(self._next_attempt_at, now)
            self._next_unauthorized_refresh_at = now + self._min_refresh_interval_s

    async def close(self) -> None:
        """Cancel refresh and any in-flight provider helper."""
        self._closed = True
        tasks = tuple(
            task
            for task in (self._refresh_task, self._attempt_task)
            if task is not None and task is not asyncio.current_task()
        )
        self._refresh_task = None
        self._attempt_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, ProviderAuthRequired):
                await task
        self._credential = None

    async def _refresh(self, *, bypass_throttle: bool = False) -> None:
        async with self._lock:
            task = self._attempt_task
            if task is not None and task.done():
                self._attempt_task = None
                task = None
            now = self._clock()
            if task is None:
                if not bypass_throttle and now < self._next_attempt_at:
                    return
                task = asyncio.create_task(self._run_helper())
                self._attempt_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._attempt_task is task:
                        self._attempt_task = None

    async def _run_helper(self) -> None:
        try:
            token = await self._helper()
            now = self._clock()
            expires_at = _authoritative_expiry(
                token,
                now=now,
                max_cache_age_s=self._max_cache_age_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - all helper and token failures are sanitized
            now = self._clock()
            self._failures += 1
            maximum_exponent = max(
                0,
                math.ceil(math.log2(self._backoff_max_s / self._backoff_base_s)),
            )
            delay = min(
                self._backoff_base_s * (2 ** min(self._failures - 1, maximum_exponent)),
                self._backoff_max_s,
            )
            self._next_attempt_at = max(
                self._next_attempt_at,
                now + self._min_refresh_interval_s,
                now + delay,
            )
            raise self._auth_required() from None

        self._credential = _Credential(token=token, expires_at=expires_at)
        self._failures = 0
        self._invalidated = False
        self._next_attempt_at = now + self._min_refresh_interval_s

    async def _refresh_loop(self) -> None:
        try:
            while not self._closed:
                credential = self._credential
                now = self._clock()
                refresh_at = (
                    credential.expires_at - self._refresh_skew_s if credential is not None else now
                )
                target = max(refresh_at, self._next_attempt_at)
                await self._sleep(max(0.0, target - now))
                if self._closed:
                    return
                with contextlib.suppress(ProviderAuthRequired):
                    await self._refresh()
        except asyncio.CancelledError:
            raise
