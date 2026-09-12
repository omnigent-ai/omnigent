"""Read-only fleet quota status for the web UI's LLMQ panel.

The controller's ``/v1/snapshot`` is an operator dump: it carries every
in-flight reservation and every agent-to-workstream mapping, which on a busy
instance is hundreds of rows naming individual sessions. The browser panel only
needs the shape of the fleet -- how full each provider window is, how the
workstream buckets are configured, and whether anything is currently being
paced -- so this route reduces the dump to that, rather than forwarding it.

Reducing here (not in the browser) keeps session identifiers off the wire, keeps
the payload roughly constant-size as the fleet grows, and gives the panel a
stable contract that survives controller-side additions to the snapshot.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._quota_controller import proxy, require_quota_admin
from omnigent.stores.permission_store import PermissionStore

_SNAPSHOT_PATH = "/v1/snapshot"
_BURST_POLICY_PATH = "/v1/burst-policy"

#: Parts per million is the controller's unit for "fraction of a window used".
_PPM = 1_000_000


class QuotaWindow(BaseModel):
    """One provider rate-limit window (e.g. Anthropic's 5-hour bucket)."""

    provider: str
    lane: str
    limit_id: str
    window_name: str
    model_scope: str
    used_ppm: int
    window_seconds: float | None
    resets_at: float | None
    hard_allowed: int | None
    source: str | None
    observed_at: float | None
    #: Live burst factor the controller is applying to this window, when it
    #: publishes one for this exact key.
    burst_factor: float | None = None


class QuotaWorkstream(BaseModel):
    """One fair-share bucket, with its current in-flight load."""

    id: str
    weight: float
    explicit_share_ppm: int | None
    active: bool
    borrow_after_seconds: float | None
    last_seen_at: float | None
    #: Reservations the controller is still holding open for this bucket.
    active_reservations: int = 0
    #: Sum of the estimated cost of those reservations, in ppm.
    active_estimated_ppm: int = 0
    #: Age of the oldest still-open reservation. A bucket that is being paced
    #: shows this climbing while ``active_reservations`` stays flat, which is
    #: the closest thing to a queue depth the controller exposes today.
    oldest_active_age_seconds: float | None = None


class QuotaBurstPolicy(BaseModel):
    """The controller's burst policy, as the slider's authoritative value."""

    initial_burst_factor: float | None = None
    max_burst_factor: float | None = None
    adaptive_enabled: bool | None = None


class QuotaStatus(BaseModel):
    """Everything the LLMQ panel renders."""

    generated_at: float
    #: Server clock at reduction time, so the browser can age the snapshot
    #: without trusting its own clock to agree with the controller's.
    observed_at: float
    windows: list[QuotaWindow]
    workstreams: list[QuotaWorkstream]
    burst: QuotaBurstPolicy
    active_reservations: int = 0


def _finite(value: Any) -> float | None:
    """Return *value* as a float when it is a real number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _burst_key(window: dict[str, Any]) -> str:
    """Rebuild the controller's ``current_burst_factors`` key for *window*.

    The controller keys its live factors by
    ``provider|lane|limit_id|window_name|model_scope|<int resets_at>``. Joining
    on that key is what lets the panel show a per-window burst factor instead of
    a single global number that does not match what any one window is doing.
    """
    resets_at = _finite(window.get("resets_at"))
    return "|".join(
        [
            str(window.get("provider") or ""),
            str(window.get("lane") or ""),
            str(window.get("limit_id") or ""),
            str(window.get("window_name") or ""),
            str(window.get("model_scope") or "*"),
            str(int(resets_at)) if resets_at is not None else "",
        ]
    )


def _reduce_windows(snapshot: dict[str, Any], burst: dict[str, Any]) -> list[QuotaWindow]:
    factors = burst.get("current_burst_factors")
    factors = factors if isinstance(factors, dict) else {}
    rows: list[QuotaWindow] = []
    for raw in snapshot.get("windows") or []:
        if not isinstance(raw, dict):
            continue
        used = _finite(raw.get("used_ppm"))
        hard_allowed = _finite(raw.get("hard_allowed"))
        rows.append(
            QuotaWindow(
                provider=str(raw.get("provider") or "unknown"),
                lane=str(raw.get("lane") or "unknown"),
                limit_id=str(raw.get("limit_id") or "unknown"),
                window_name=str(raw.get("window_name") or "unknown"),
                model_scope=str(raw.get("model_scope") or "*"),
                used_ppm=max(0, int(used)) if used is not None else 0,
                window_seconds=_finite(raw.get("window_seconds")),
                resets_at=_finite(raw.get("resets_at")),
                hard_allowed=int(hard_allowed) if hard_allowed is not None else None,
                source=str(raw["source"]) if raw.get("source") else None,
                observed_at=_finite(raw.get("observed_at")),
                burst_factor=_finite(factors.get(_burst_key(raw))),
            )
        )
    # Fullest first: the window about to throttle the fleet is the one worth
    # putting at the top of the panel.
    rows.sort(key=lambda row: (-row.used_ppm, row.provider, row.lane, row.window_name))
    return rows


def _reduce_workstreams(snapshot: dict[str, Any], now: float) -> list[QuotaWorkstream]:
    buckets: dict[str, QuotaWorkstream] = {}
    for raw in snapshot.get("workstreams") or []:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("id") or "")
        if not identifier:
            continue
        share = _finite(raw.get("explicit_share_ppm"))
        buckets[identifier] = QuotaWorkstream(
            id=identifier,
            weight=_finite(raw.get("weight")) or 1.0,
            explicit_share_ppm=int(share) if share is not None else None,
            active=bool(raw.get("active")),
            borrow_after_seconds=_finite(raw.get("borrow_after_seconds")),
            last_seen_at=_finite(raw.get("last_seen_at")),
        )

    for raw in snapshot.get("reservations") or []:
        if not isinstance(raw, dict) or raw.get("status") != "active":
            continue
        bucket = buckets.get(str(raw.get("workstream") or ""))
        if bucket is None:
            continue
        estimated = _finite(raw.get("estimated_ppm"))
        created_at = _finite(raw.get("created_at"))
        bucket.active_reservations += 1
        bucket.active_estimated_ppm += max(0, int(estimated)) if estimated is not None else 0
        if created_at is not None:
            # Reservation clocks come from the controller host; a future
            # timestamp means clock skew, not a negative-age reservation.
            age = max(0.0, now - created_at)
            if bucket.oldest_active_age_seconds is None or age > bucket.oldest_active_age_seconds:
                bucket.oldest_active_age_seconds = age

    # Busiest first, then idle buckets alphabetically.
    return sorted(
        buckets.values(),
        key=lambda row: (-row.active_reservations, -row.weight, row.id),
    )


def _reduce(snapshot: dict[str, Any], burst: dict[str, Any], now: float) -> QuotaStatus:
    workstreams = _reduce_workstreams(snapshot, now)
    return QuotaStatus(
        generated_at=_finite(snapshot.get("generated_at")) or now,
        observed_at=now,
        windows=_reduce_windows(snapshot, burst),
        workstreams=workstreams,
        burst=QuotaBurstPolicy(
            initial_burst_factor=_finite(burst.get("initial_burst_factor")),
            max_burst_factor=_finite(burst.get("max_burst_factor")),
            adaptive_enabled=(
                burst.get("adaptive_enabled")
                if isinstance(burst.get("adaptive_enabled"), bool)
                else None
            ),
        ),
        active_reservations=sum(row.active_reservations for row in workstreams),
    )


def create_quota_status_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Create the browser-facing read-only quota status route."""
    router = APIRouter()

    @router.get("/quota/status")
    async def get_quota_status(request: Request) -> QuotaStatus:
        await require_quota_admin(request, auth_provider, permission_store)
        # The two reads are independent; the panel wants them from the same
        # instant, so fetch them together rather than one after the other.
        snapshot, burst = await asyncio.gather(
            proxy("GET", _SNAPSHOT_PATH, timeout=15.0),
            proxy("GET", _BURST_POLICY_PATH),
        )
        return _reduce(snapshot, burst, time.time())

    return router


__all__ = ["QuotaStatus", "create_quota_status_router"]
