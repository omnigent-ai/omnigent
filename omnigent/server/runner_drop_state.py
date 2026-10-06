"""How each runner's tunnel last ended, and the extra grace a silent drop earns.

A laptop that sleeps or loses its network goes quiet without closing its socket,
and the runner on it usually reconnects on wake. A runner that is gone for good
closes or resets its socket instead. The tunnel route records which it saw
before the disconnect callback runs, so the two disconnect paths (the per-runner
grace timer and the session relay) can hold a silently dropped runner's turn
open while its host is also away, and fail a vanished runner on the normal grace.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

DropKind = Literal["silent", "sudden"]
GraceOutcome = Literal["reconnected", "host_back_runner_missing", "expired", "superseded"]

# Longer than any grace that reads a record; older ones belong to runners that never returned.
_RETAIN_S = 3600.0


@dataclass(frozen=True)
class RunnerDrop:
    """How a runner's tunnel last ended.

    :param kind: ``"silent"`` when the tunnel went quiet for nearly the keepalive
        window (sleep, network blackhole); ``"sudden"`` for any other end.
    :param dropped_at: :func:`time.monotonic` reading when the server saw it end.
    """

    kind: DropKind
    dropped_at: float


@dataclass(frozen=True)
class SilentGraceEnd:
    """How a silently dropped runner's grace ended once the normal grace had run out.

    :param outcome: ``"reconnected"`` when the runner re-registered,
        ``"host_back_runner_missing"`` when its host returned without it, or
        ``"expired"`` when the grace ran out.
    :param host_online: Whether the host was online when the grace ended.
    :param extended: Whether the wait ran past the normal grace. ``False`` when
        the host was already online, so the normal decision applies at once.
    """

    outcome: Literal["reconnected", "host_back_runner_missing", "expired"]
    host_online: bool
    extended: bool


_lock = threading.Lock()
# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally-unique runner_id
_drops: dict[str, RunnerDrop] = {}
# Blocking ``host_id -> online`` lookup wired by ``create_app``; ``None`` reads hosts as offline.
_host_online_probe: Callable[[str], bool] | None = None


def note(runner_id: str, kind: DropKind) -> RunnerDrop:
    """Record how *runner_id*'s tunnel just ended, replacing any earlier record.

    :param runner_id: Runner whose tunnel ended, e.g. ``"runner_0123456789abcdef"``.
    :param kind: Classification of the end.
    :returns: The stored record.
    """
    now = time.monotonic()
    drop = RunnerDrop(kind=kind, dropped_at=now)
    with _lock:
        for stale in [rid for rid, old in _drops.items() if now - old.dropped_at > _RETAIN_S]:
            del _drops[stale]
        _drops[runner_id] = drop
    return drop


def get(runner_id: str | None) -> RunnerDrop | None:
    """Return the record of *runner_id*'s last tunnel end, if it has not reconnected.

    :param runner_id: Runner to look up; ``None`` (a relay with no runner binding) has none.
    :returns: The record, or ``None`` when the runner reconnected or never dropped.
    """
    if runner_id is None:
        return None
    with _lock:
        return _drops.get(runner_id)


def clear(runner_id: str) -> None:
    """Forget *runner_id*'s record once its tunnel is back.

    :param runner_id: Runner that just registered.
    """
    with _lock:
        _drops.pop(runner_id, None)


def reset_for_tests() -> None:
    """Drop every record (test isolation)."""
    with _lock:
        _drops.clear()


def configure_host_probe(probe: Callable[[str], bool] | None) -> None:
    """Wire (or clear) the cross-replica host liveness lookup.

    :param probe: Blocking ``host_id -> online`` check, run off the event loop.
        ``None`` reads every host as offline.
    """
    global _host_online_probe
    _host_online_probe = probe


async def host_is_online(host_id: str | None) -> bool:
    """Return whether *host_id* is live, counting an unresolvable host as offline.

    :param host_id: Host bound to the runner, or ``None`` when it cannot be resolved.
    :returns: ``True`` only when the probe confirms the host is online.
    """
    probe = _host_online_probe
    if host_id is None or probe is None:
        return False
    try:
        return bool(await asyncio.to_thread(probe, host_id))
    except Exception:  # noqa: BLE001 - an unreadable host row must not fail a turn early
        _logger.warning("Host liveness check failed for host=%s", host_id, exc_info=True)
        return False


async def hold_for_silent_drop(
    drop: RunnerDrop | None,
    *,
    grace_s: float,
    recheck_s: float,
    wait_for_runner: Callable[[float], Awaitable[bool]],
    bound_host_ids: Callable[[], Awaitable[Sequence[str]]],
) -> SilentGraceEnd | None:
    """Keep a silently dropped runner's turn open while its host is also away.

    Called once the normal grace has run out. Applies only to a silent drop that
    is not past *grace_s* and whose runner is still absent. If its host is online
    the normal decision stands; otherwise (an unresolvable host counts as offline)
    it waits, event-driven, for the runner to re-register, rechecking the host every
    *recheck_s*. A host that returns without its runner gets one more recheck before
    the wait ends: a host that woke without its runner means the runner is gone.

    :param drop: The runner's last drop record.
    :param grace_s: Total silent-drop grace, measured from the drop.
    :param recheck_s: Seconds between host rechecks.
    :param wait_for_runner: Waits up to the given seconds for the runner to
        re-register; ``True`` when it is registered. ``0`` checks without waiting.
    :param bound_host_ids: Resolves the host(s) the runner's sessions are bound to.
    :returns: How the grace ended, or ``None`` when this drop earns no extra grace
        and its host was not consulted.
    """
    if drop is None or drop.kind != "silent":
        return None
    remaining = grace_s - (time.monotonic() - drop.dropped_at)
    # A registered runner has not dropped; its stream failed for another reason.
    if remaining <= 0 or await wait_for_runner(0.0):
        return None
    try:
        host_ids = await bound_host_ids()
    except Exception:  # noqa: BLE001 - an unresolved host is treated as offline
        _logger.warning("Could not resolve the host of a silently dropped runner", exc_info=True)
        host_ids = ()

    async def host_up() -> bool:
        return any([await host_is_online(host_id) for host_id in host_ids])

    if await host_up():
        return SilentGraceEnd("expired", host_online=True, extended=False)
    deadline = time.monotonic() + remaining
    host_back = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return SilentGraceEnd("expired", host_online=host_back, extended=True)
        if await wait_for_runner(min(max(recheck_s, 0.01), left)):
            return SilentGraceEnd("reconnected", host_online=host_back, extended=True)
        was_back, host_back = host_back, await host_up()
        if was_back and host_back:
            return SilentGraceEnd("host_back_runner_missing", host_online=True, extended=True)


def log_grace_end(
    *,
    path: Literal["timer", "relay"],
    runner_id: str | None,
    session_id: str | None,
    drop: RunnerDrop | None,
    outcome: GraceOutcome,
    grace_s: float,
    waited_s: float,
    extended: bool,
    host_online: bool | None,
) -> None:
    """Emit the ``runner_disconnect_grace`` row for one ended disconnect grace.

    :param path: Which wait ended: the per-runner ``timer`` or a session's ``relay``.
    :param runner_id: The disconnected runner, when known.
    :param session_id: The relay's session; ``None`` for the per-runner timer.
    :param drop: The runner's drop record, when one was noted.
    :param outcome: How the grace ended.
    :param grace_s: The grace that applied: the silent-drop grace once extended.
    :param waited_s: Seconds from the drop to the end of the grace.
    :param extended: Whether the wait ran past the normal grace.
    :param host_online: Whether the host was online when the grace ended, or
        ``None`` when it was never checked.
    """
    _logger.log(
        logging.INFO if outcome in ("reconnected", "superseded") else logging.WARNING,
        "Runner %s disconnect grace ended via %s: %s (drop=%s extended=%s waited=%.1fs)",
        runner_id,
        path,
        outcome,
        drop.kind if drop is not None else "unknown",
        extended,
        waited_s,
        extra=debug_event(
            "runner_disconnect_grace",
            session_id=session_id,
            runner_id=runner_id,
            path=path,
            drop_kind=drop.kind if drop is not None else None,
            host_online=host_online,
            grace_s=grace_s,
            extended=extended,
            waited_s=round(waited_s, 3),
            outcome=outcome,
        ),
    )
