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
from typing import Literal, TypeVar

from omnigent.db.db_models import current_workspace_id
from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_D = TypeVar("_D")

DropKind = Literal["silent", "sudden"]
GraceOutcome = Literal[
    "reconnected", "host_back_runner_missing", "live_elsewhere", "expired", "superseded"
]

# Longer than any grace that reads a record; older ones belong to runners that never returned.
_RETAIN_S = 3600.0
# How long one host's liveness answer is reused, so the holds of a laptop's many sessions
# recheck it together instead of once each.
_PROBE_TTL_S = 10.0


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
    """How a held silent drop ended once the normal grace had run out.

    :param outcome: ``"reconnected"`` when the runner re-registered,
        ``"host_back_runner_missing"`` when its host was back without it,
        ``"live_elsewhere"`` when it re-registered on another replica, or
        ``"expired"`` when the grace ran out.
    :param host_online: Whether the host was online when the hold ended.
    """

    outcome: Literal["reconnected", "host_back_runner_missing", "live_elsewhere", "expired"]
    host_online: bool


_lock = threading.Lock()
# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally-unique runner_id
_drops: dict[str, RunnerDrop] = {}
# Host answers and the lookups in flight, keyed by (workspace_id, host_id) like the host registry.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by (workspace_id, host_id)
_probe_cache: dict[tuple[int, str], tuple[float, bool]] = {}
# custom-lint: disable-next=workspace-scoped-cache -- keyed by (workspace_id, host_id)
_probe_inflight: dict[tuple[int, str], asyncio.Future[bool]] = {}
# Blocking ``host_id -> online`` and ``host_id -> managed sandbox`` lookups wired by
# ``create_app``; unwired, every host reads as offline and none as managed.
_host_online_probe: Callable[[str], bool] | None = None
_host_managed_probe: Callable[[str], bool] | None = None


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
    """Drop every record and cached host answer (test isolation)."""
    with _lock:
        _drops.clear()
        _probe_cache.clear()
        _probe_inflight.clear()


def configure_host_probe(
    probe: Callable[[str], bool] | None,
    *,
    is_managed: Callable[[str], bool] | None = None,
) -> None:
    """Wire (or clear) the cross-replica host lookups.

    :param probe: Blocking ``host_id -> online`` check, run off the event loop.
        ``None`` reads every host as offline.
    :param is_managed: Blocking ``host_id -> server-managed sandbox`` check, run
        off the event loop. ``None`` reads every host as a machine that can wake.
    """
    global _host_online_probe, _host_managed_probe
    _host_online_probe = probe
    _host_managed_probe = is_managed


async def host_is_online(host_id: str | None, *, timeout_s: float | None = None) -> bool:
    """Return whether *host_id* is live, counting an unresolvable host as offline.

    Answers are reused for :data:`_PROBE_TTL_S`, and concurrent callers for one host
    share a single lookup. A lookup that fails or outlives *timeout_s* reads as
    offline and is not reused.

    :param host_id: Host bound to the runner, or ``None`` when it cannot be resolved.
    :param timeout_s: Longest to wait for the lookup, e.g. the time left in a hold.
    :returns: ``True`` only when the probe confirms the host is online.
    """
    probe = _host_online_probe
    if host_id is None or probe is None:
        return False
    key = (current_workspace_id(), host_id)
    loop = asyncio.get_running_loop()
    with _lock:
        hit = _probe_cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        shared = _probe_inflight.get(key)
        owner = shared is None or shared.get_loop() is not loop
        if owner:
            shared = loop.create_future()
            _probe_inflight[key] = shared
    assert shared is not None
    if not owner:
        try:
            return await asyncio.wait_for(asyncio.shield(shared), timeout_s)
        except asyncio.TimeoutError:
            return False
    online = False
    try:
        online = bool(await asyncio.wait_for(asyncio.to_thread(probe, host_id), timeout_s))
        with _lock:
            _probe_cache[key] = (time.monotonic() + _PROBE_TTL_S, online)
    except asyncio.TimeoutError:
        _logger.warning("Host liveness check for host=%s outlived the hold", host_id)
    except Exception:  # noqa: BLE001 - an unreadable host row must not fail a turn early
        _logger.warning("Host liveness check failed for host=%s", host_id, exc_info=True)
    finally:
        with _lock:
            if _probe_inflight.get(key) is shared:
                del _probe_inflight[key]
        if not shared.done():
            shared.set_result(online)
    return online


async def host_is_managed(host_id: str | None, *, timeout_s: float | None = None) -> bool:
    """Return whether *host_id* is a server-managed sandbox, which cannot wake on its own.

    A lookup that fails reads as managed: a host that cannot be confirmed to be a
    laptop is not held for.

    :param host_id: Host bound to the runner, or ``None`` when it cannot be resolved.
    :param timeout_s: Longest to wait for the lookup.
    :returns: ``True`` for a managed sandbox host.
    """
    probe = _host_managed_probe
    if host_id is None or probe is None:
        return False
    try:
        return bool(await asyncio.wait_for(asyncio.to_thread(probe, host_id), timeout_s))
    except Exception:  # noqa: BLE001 - unknown eligibility keeps the normal grace
        _logger.warning("Managed-host check failed for host=%s", host_id, exc_info=True)
        return True


async def _bounded(
    call: Callable[[], Awaitable[_T]], default: _D, *, deadline: float, what: str
) -> _T | _D:
    """Await *call* inside the hold window; a slow or failing lookup yields *default*."""
    try:
        return await asyncio.wait_for(call(), max(deadline - time.monotonic(), 0.0))
    except asyncio.TimeoutError:
        _logger.warning("Silent-drop %s outlived the hold window", what)
    except Exception:  # noqa: BLE001 - a failed lookup must not fail the turn early
        _logger.warning("Silent-drop %s failed", what, exc_info=True)
    return default


async def _any_host(
    check: Callable[..., Awaitable[bool]], host_ids: Sequence[str], *, deadline: float
) -> bool:
    """Ask *check* about each host in turn until one says yes or the window closes.

    Each call gets only the time left, so the hosts share one deadline however many
    there are, and the first yes skips the rest.
    """
    for host_id in host_ids:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        if await check(host_id, timeout_s=left):
            return True
    return False


async def hold_for_silent_drop(
    drop: RunnerDrop | None,
    *,
    grace_s: float,
    recheck_s: float,
    wait_for_runner: Callable[[float], Awaitable[bool]],
    turn_at_stake: Callable[[], Awaitable[bool]],
    bound_host_ids: Callable[[], Awaitable[Sequence[str]]],
    runner_live_elsewhere: Callable[[], Awaitable[bool]],
) -> SilentGraceEnd | None:
    """Keep a silently dropped runner's turn open while its host is also away.

    Called once the normal grace has run out. Applies only to a silent drop that
    is not past *grace_s* (``0`` turns the hold off), whose runner is still absent
    and not live on another replica, that has a mid-turn session to lose, and
    whose host is not a managed sandbox. An unresolvable host counts as offline; a
    failed host lookup is retried at each recheck, and the hosts it finds then get
    the same sandbox check.

    It then waits, event-driven, for the runner to re-register, rechecking the host
    every *recheck_s*. A host that is back, including one already online when the
    normal grace ended, gets one more recheck for its runner to follow: a host that
    woke without its runner means the runner is gone.

    Every lookup is bounded by the time left in the window, and a runner's hosts
    share that time between them.

    :param drop: The runner's last drop record.
    :param grace_s: Total silent-drop grace, measured from the drop.
    :param recheck_s: Seconds between host rechecks.
    :param wait_for_runner: Waits up to the given seconds for the runner to
        re-register; ``True`` when it is registered. ``0`` checks without waiting.
    :param turn_at_stake: Whether a bound session is mid-turn and worth holding for.
    :param bound_host_ids: Resolves the host(s) serving the sessions at stake.
    :param runner_live_elsewhere: Whether another replica now holds the runner.
    :returns: How the hold ended, or ``None`` when this drop earns no extra grace,
        including when a host found late proves to be a managed sandbox.
    """
    if drop is None or drop.kind != "silent" or grace_s <= 0:
        return None
    deadline = drop.dropped_at + grace_s
    # A registered runner has not dropped; its stream failed for another reason.
    if deadline <= time.monotonic() or await wait_for_runner(0.0):
        return None
    if not await _bounded(turn_at_stake, False, deadline=deadline, what="turn check"):
        _logger.info("Silent drop not held: no mid-turn session is bound to the runner")
        return None
    if await _bounded(runner_live_elsewhere, False, deadline=deadline, what="liveness check"):
        _logger.info("Silent drop not held: the runner is live on another replica")
        return None

    host_ids: Sequence[str] | None = None

    async def host_is_sandbox() -> bool:
        """Look the hosts up until a lookup succeeds; whether one of them is a sandbox."""
        nonlocal host_ids
        if host_ids is not None:
            return False
        host_ids = await _bounded(bound_host_ids, None, deadline=deadline, what="host lookup")
        return host_ids is not None and await _any_host(
            host_is_managed, host_ids, deadline=deadline
        )

    async def host_up() -> bool:
        return await _any_host(host_is_online, host_ids or (), deadline=deadline)

    if await host_is_sandbox():
        _logger.info("Silent drop not held: the host is a managed sandbox")
        return None
    host_back = await host_up()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return SilentGraceEnd("expired", host_online=host_back)
        if await wait_for_runner(min(max(recheck_s, 0.01), left)):
            return SilentGraceEnd("reconnected", host_online=host_back)
        if await _bounded(runner_live_elsewhere, False, deadline=deadline, what="liveness check"):
            return SilentGraceEnd("live_elsewhere", host_online=host_back)
        if await host_is_sandbox():
            _logger.info("Silent drop no longer held: the host is a managed sandbox")
            return None
        was_back, host_back = host_back, await host_up()
        if was_back and host_back:
            return SilentGraceEnd("host_back_runner_missing", host_online=True)


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
