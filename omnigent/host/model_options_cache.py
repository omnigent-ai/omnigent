"""Host-side cache for pre-launch harness model listings.

The listings behind ``host.model_options`` are resolved by probing the real
harness binaries, which costs seconds cold. This layer keeps the request
path instant: results are cached per harness under a configuration
fingerprint, served stale-while-revalidating (a stale or reconfigured entry
is returned immediately while a single-flight background probe refreshes
it), and prewarmed at host startup so the first picker open is warm. Only a
true cold miss awaits a probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from omnigent.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

# Refresh cadence for a served entry; matches the codex model-discovery
# cache so the two layers age together.
_DEFAULT_TTL_S = 300.0
# After a failed background refresh, keep serving the stale entry without
# re-kicking a probe for this long — a broken harness must not be probed
# once per picker request.
_REFRESH_FAILURE_BACKOFF_S = 30.0


def fingerprint_of(*parts: object) -> str:
    """
    Stable fingerprint of a resolved harness configuration.

    :param parts: Hashable configuration facets — resolved overrides, env
        pairs, binary identity. Stringified in order.
    :returns: A short hex digest.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class ModelOptionsResult:
    """
    One harness's resolved pre-launch listing.

    :param models: Picker rows in the harness's own shape.
    :param routable_models: Every id the harness's endpoint routes.
    """

    models: list[_JsonObject]
    routable_models: list[str]


@dataclass
class _Entry:
    """A cached listing plus the configuration it was resolved under."""

    fingerprint: str
    result: ModelOptionsResult
    resolved_at: float
    failed_refresh_at: float | None = None


@dataclass
class ModelOptionsCache:
    """
    Per-harness listing cache with single-flight refresh.

    :param ttl_s: Age at which a fingerprint-matching entry starts a
        background refresh (it is still served immediately).
    """

    ttl_s: float = _DEFAULT_TTL_S
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _inflight: dict[str, asyncio.Task[ModelOptionsResult]] = field(default_factory=dict)

    async def get(
        self,
        harness: str,
        *,
        fingerprint: str,
        resolve: Callable[[], Awaitable[ModelOptionsResult]],
    ) -> ModelOptionsResult:
        """
        Return the harness's listing, probing only when unavoidable.

        Fresh fingerprint-matching entry → served as-is. Stale or
        reconfigured entry → served as-is while one background refresh runs.
        No entry at all → awaits the (single-flight) probe.

        :param harness: Cache key, e.g. ``"codex-native"``.
        :param fingerprint: The current configuration fingerprint.
        :param resolve: Probe coroutine factory producing the fresh listing.
        :returns: The cached or freshly probed listing.
        :raises Exception: Whatever *resolve* raises, on a cold miss only.
        """
        entry = self._entries.get(harness)
        now = time.monotonic()
        if entry is not None:
            fresh = entry.fingerprint == fingerprint and now - entry.resolved_at < self.ttl_s
            if not fresh and self._should_refresh(entry, now):
                self._kick_refresh(harness, fingerprint, resolve)
            return entry.result
        task = self._ensure_inflight(harness, fingerprint, resolve)
        return await asyncio.shield(task)

    async def prewarm(
        self,
        harness: str,
        *,
        fingerprint: str,
        resolve: Callable[[], Awaitable[ModelOptionsResult]],
    ) -> None:
        """
        Best-effort warm-up of one harness's listing.

        :param harness: Cache key.
        :param fingerprint: The current configuration fingerprint.
        :param resolve: Probe coroutine factory.
        :returns: None. Probe failures are logged, never raised.
        """
        try:
            await self.get(harness, fingerprint=fingerprint, resolve=resolve)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — prewarm is opportunistic by design
            _logger.info("model-options prewarm failed for %s", harness, exc_info=True)

    def close(self) -> None:
        """
        Cancel any in-flight probes (host shutdown).

        :returns: None.
        """
        for task in self._inflight.values():
            task.cancel()
        self._inflight.clear()

    def _should_refresh(self, entry: _Entry, now: float) -> bool:
        """Whether a stale entry may kick a refresh (failure backoff)."""
        return (
            entry.failed_refresh_at is None
            or now - entry.failed_refresh_at >= _REFRESH_FAILURE_BACKOFF_S
        )

    def _ensure_inflight(
        self,
        harness: str,
        fingerprint: str,
        resolve: Callable[[], Awaitable[ModelOptionsResult]],
    ) -> asyncio.Task[ModelOptionsResult]:
        """Return the harness's single-flight probe task, starting one if idle."""
        task = self._inflight.get(harness)
        if task is not None and not task.done():
            return task

        async def _run() -> ModelOptionsResult:
            try:
                result = await resolve()
            except asyncio.CancelledError:
                raise
            except BaseException:
                entry = self._entries.get(harness)
                if entry is not None:
                    entry.failed_refresh_at = time.monotonic()
                raise
            self._entries[harness] = _Entry(
                fingerprint=fingerprint,
                result=result,
                resolved_at=time.monotonic(),
            )
            return result

        task = asyncio.create_task(_run(), name=f"model-options-probe-{harness}")
        self._inflight[harness] = task
        return task

    def _kick_refresh(
        self,
        harness: str,
        fingerprint: str,
        resolve: Callable[[], Awaitable[ModelOptionsResult]],
    ) -> None:
        """Start (or join) a background refresh without awaiting it."""
        task = self._ensure_inflight(harness, fingerprint, resolve)

        def _swallow(done: asyncio.Task[ModelOptionsResult]) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done.exception()
                if exc is not None:
                    _logger.warning("model-options refresh failed for %s: %s", harness, exc)

        task.add_done_callback(_swallow)
