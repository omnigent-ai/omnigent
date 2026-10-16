"""Keep a managed sandbox warm while its agent is still running.

The runner tunnel's ping loop already stamps ``runner_last_seen`` every
``PING_INTERVAL_S`` for as long as a runner holds a live tunnel, and a live
runner tunnel is the server's signal that an agent is still working on that
sandbox: the runner exits itself once idle (``runner.idle_timeout_s``), so the
signal disappears on its own when the session goes quiet.

This module turns that existing signal into a provider
:meth:`~omnigent.onboarding.sandboxes.base.SandboxHostLauncher.keep_alive` call,
so a sandbox whose platform reaps on inactivity, or one that expects the
operator to push an absolute deadline forward, stays up while work is happening
and is reclaimed once it is not. Nine providers already implement ``keep_alive``
but only the CLI bootstrap ever called it; this is the managed-path caller.

Providers that cannot extend a sandbox (``kubernetes`` today) raise
:class:`SandboxCapabilityError` and are skipped, leaving their behaviour exactly
as it is now.

Rate-limited per runner at a provider-scoped cadence
(:func:`~omnigent.onboarding.sandboxes.base.resolve_managed_keepalive_interval_s`):
``keep_alive`` is a provider API call — on Kubernetes-style backends an apiserver
write that wakes a controller reconcile — so agent_sandbox refreshes fast (short
window) while other providers stay on the cheap default.

Refreshes run on a bounded worker pool. A runner has at most one queued or
running refresh, and its throttle timestamp is recorded when a worker starts
rather than when a job enters the queue. This keeps one stalled provider from
starving unrelated live runners while retaining the existing active-tunnel gate.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from typing import TYPE_CHECKING

from omnigent.debug_logging import debug_event
from omnigent.onboarding.sandboxes.base import (
    SandboxCapabilityError,
    resolve_managed_keepalive_interval_s,
)

if TYPE_CHECKING:
    from omnigent.server.managed_hosts import ManagedSandboxDeployment
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

# Keep provider I/O isolated from the tunnel event loop without allowing one
# stalled provider call to serialize every active runner's refresh.
_KEEPALIVE_MAX_WORKERS = 8
# Retry an early/queued tick without polling the worker pool aggressively.
_KEEPALIVE_RETRY_DELAY_S = 1.0


class _KeepAliveOutcome(StrEnum):
    """Bounded outcomes emitted by one managed-sandbox refresh attempt."""

    EXTENDED = "extended"
    SOFT_FAILED = "soft_failed"
    UNSUPPORTED = "unsupported"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NO_SANDBOX = "no_sandbox"
    NO_HOST = "no_host"
    PROVIDER_ERROR = "provider_error"
    RESOLUTION_ERROR = "resolution_error"
    SUBMISSION_FAILED = "submission_failed"


# runner_id -> its provider's keepalive cadence (seconds), filled by
# _keep_alive_for_runner once the runner's provider is resolved. Until then the
# fast agent_sandbox cadence is used (see _interval_for) so an agent_sandbox's
# short window is never under-refreshed; a slower provider self-corrects to its
# own cadence after its first keepalive, at the cost of one early refresh.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_runner_interval_s: dict[str, float] = {}

# Growth threshold for pruning expired scheduling state, not a hard size cap.
# Unexpired entries are retained so active runners keep their retry backoff.
_THROTTLE_MAX_ENTRIES = 4096

_conversation_store: ConversationStore | None = None
_host_store: HostStore | None = None
_sandbox_config: ManagedSandboxDeployment | None = None
_executor: ThreadPoolExecutor | None = None

# runner_id -> monotonic seconds when its last worker actually started.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_last_kept: dict[str, float] = {}

# Runners with work queued or running on the executor. The throttle alone bounds
# the queue only while calls finish inside the interval; this also keeps a stalled
# provider from stacking a second job for the same runner behind the first.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_inflight: set[str] = set()
# runner_id -> monotonic time before which another submission should not be
# attempted after executor rejection or cancellation.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_retry_after: dict[str, float] = {}
_state_lock = threading.Lock()
# Kept as a named alias for tests and callers that inspect the single-flight
# guard. All shared throttle, cadence, and reservation state uses this lock.
_inflight_lock = _state_lock

# Set only on a worker, after it leaves the executor queue. Direct unit-test
# calls to _keep_alive_for_runner have no queue delay, so the field is omitted.
_queue_delay_s: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "managed_keepalive_queue_delay_s", default=None
)


def configure(
    conversation_store: ConversationStore,
    host_store: HostStore | None,
    sandbox_config: ManagedSandboxDeployment | None,
) -> None:
    """Wire the stores and provider set the keepalive needs.

    Called once at app construction. A ``None`` *sandbox_config* (no
    ``sandbox:`` section) leaves :func:`touch` a no-op, so a server with no
    managed sandboxes pays nothing.

    :param conversation_store: Store used to resolve a runner to its session's host.
    :param host_store: Store used to read the host's recorded sandbox; ``None``
        (a server built without one) also disables the hook.
    :param sandbox_config: The deployment's provider set, or ``None`` when
        managed sandboxes are not configured.
    """
    global _conversation_store, _host_store, _sandbox_config, _executor
    _conversation_store = conversation_store
    _host_store = host_store
    _sandbox_config = sandbox_config
    with _state_lock:
        if sandbox_config is not None and host_store is not None and _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_KEEPALIVE_MAX_WORKERS,
                thread_name_prefix="managed-keepalive",
            )


def _interval_for(runner_id: str) -> float:
    """The keepalive cadence for *runner_id*: its provider's, once cached, else
    the fast agent_sandbox cadence so a short-window sandbox is never under-refreshed."""
    with _state_lock:
        interval = _runner_interval_s.get(runner_id)
    return interval or resolve_managed_keepalive_interval_s("agent_sandbox")


def keepalive_interval_s(runner_id: str) -> float:
    """Return the provider cadence used by *runner_id*'s keepalive scheduling.

    Provider-scoped: agent_sandbox refreshes fast because its window is short;
    other providers keep the cheaper default so they are not over-called. The loop
    uses :func:`next_keepalive_delay_s` to sleep the remaining cadence or a
    bounded retry delay, while the per-runner throttle in :func:`touch` uses this
    provider interval.
    """
    return _interval_for(runner_id)


def next_keepalive_delay_s(runner_id: str, *, now: float | None = None) -> float:
    """Return the next runner-local wake delay from the worker-start stamp.

    The tunnel loop wakes at the remaining provider interval once a worker has
    started. An early tick, or a provider call still queued/running, retries at
    a bounded short delay instead of deferring the refresh for another full
    cadence. No provider or sandbox policy is changed here.

    :param runner_id: Runner whose refresh schedule is being advanced.
    :param now: Optional monotonic timestamp for deterministic tests.
    :returns: Non-negative seconds until the next ``touch`` call.
    """
    current = time.monotonic() if now is None else now
    interval = _interval_for(runner_id)
    managed_keepalive_enabled = (
        _sandbox_config is not None and _host_store is not None and _executor is not None
    )
    with _state_lock:
        last = _last_kept.get(runner_id)
        inflight = runner_id in _inflight
        retry_after = _retry_after.get(runner_id)
    if not managed_keepalive_enabled:
        # A stale worker-start stamp must not make a disabled hook spin at zero.
        delay = interval
    elif retry_after is not None:
        remaining_retry = retry_after - current
        delay = remaining_retry if remaining_retry > 0 else _KEEPALIVE_RETRY_DELAY_S
    elif last is None:
        delay = _KEEPALIVE_RETRY_DELAY_S if inflight else interval
    else:
        remaining = interval - (current - last)
        delay = remaining if remaining > 0 else _KEEPALIVE_RETRY_DELAY_S
    _logger.debug(
        "managed sandbox keepalive scheduled in %.3fs for runner %s",
        delay,
        runner_id,
        extra=debug_event(
            "managed_keepalive_schedule",
            runner_id=runner_id,
            interval_s=round(interval, 3),
            delay_s=round(max(0.0, delay), 3),
            inflight=inflight,
            retry_after_s=(
                round(max(0.0, retry_after - current), 3) if retry_after is not None else None
            ),
            worker_start_age_s=(round(max(0.0, current - last), 3) if last is not None else None),
        ),
    )
    return max(0.0, delay)


def touch(runner_id: str) -> None:
    """Keep the sandbox behind *runner_id* warm, at most every :func:`keepalive_interval_s`.

    Non-blocking and fail-safe: the provider call runs on a worker thread so a
    slow backend cannot delay the tunnel ping loop that calls this, and every
    failure is logged and swallowed. Safe to call on every ping.

    The worker runs inside a snapshot of THIS caller's ``contextvars``
    (``copy_context().run``), which is load-bearing rather than tidiness: the
    resolution chain reads stores that filter every query on
    ``current_workspace_id()``, a ``ContextVar`` the multi-tenant request
    middleware binds per request via ``workspace_scope``. A bare
    ``ThreadPoolExecutor.submit`` would run those reads at the default workspace
    (0), so on a multi-tenant replica they would match no rows and the keepalive
    would silently no-op, letting a busy sandbox reclaim itself mid-run. Same
    guard, and same reason, as :func:`omnigent.server.session_live_state._submit`
    on the other side of this ping loop.

    :param runner_id: Runner with a live tunnel, e.g. ``"runner_token_abc"``.
    """
    executor = _executor
    if _sandbox_config is None or _host_store is None or executor is None:
        return
    now = time.monotonic()
    interval = _interval_for(runner_id)
    with _state_lock:
        last = _last_kept.get(runner_id)
        retry_after = _retry_after.get(runner_id)
        if retry_after is not None and now >= retry_after:
            _retry_after.pop(runner_id, None)
            retry_after = None
        if retry_after is not None and now < retry_after:
            return
        if last is not None and now - last < interval:
            return
        if runner_id in _inflight:
            # A reservation spans both queued and running work. Do not let a
            # slow provider accumulate duplicate jobs for one runner.
            return
        _inflight.add(runner_id)
        should_prune = max(len(_last_kept), len(_retry_after)) > _THROTTLE_MAX_ENTRIES
    if should_prune:
        _prune_throttle(now)
    try:
        ctx = contextvars.copy_context()
        future = executor.submit(ctx.run, _run_keepalive_job, runner_id, now)
    except Exception as exc:  # noqa: BLE001 - submission is best effort
        with _state_lock:
            _inflight.discard(runner_id)
            _retry_after[runner_id] = now + _KEEPALIVE_RETRY_DELAY_S
            should_prune = len(_retry_after) > _THROTTLE_MAX_ENTRIES
        if should_prune:
            _prune_throttle(now)
        _emit_outcome(
            runner_id,
            _KeepAliveOutcome.SUBMISSION_FAILED,
            error_type=_bounded_error_type(exc),
            queue_delay_s=0.0,
        )
        return
    with _state_lock:
        _retry_after.pop(runner_id, None)
    future.add_done_callback(
        lambda completed: ctx.run(_release_cancelled_job, completed, runner_id)
    )


def _prune_throttle(now: float) -> None:
    """Drop stale throttle entries and expired submission-retry deadlines."""
    cutoff = now - 2 * resolve_managed_keepalive_interval_s()
    with _state_lock:
        stale = [rid for rid, seen in _last_kept.items() if seen < cutoff]
        for runner_id in stale:
            _last_kept.pop(runner_id, None)
            _runner_interval_s.pop(runner_id, None)
        for runner_id, retry_after in list(_retry_after.items()):
            if retry_after <= now:
                _retry_after.pop(runner_id, None)


def _release_cancelled_job(future: Future[None], runner_id: str) -> None:
    """Release a reservation when an accepted executor job is cancelled."""
    if future.cancelled():
        retry_at = time.monotonic()
        with _state_lock:
            _inflight.discard(runner_id)
            _retry_after[runner_id] = retry_at + _KEEPALIVE_RETRY_DELAY_S
            should_prune = len(_retry_after) > _THROTTLE_MAX_ENTRIES
        if should_prune:
            _prune_throttle(retry_at)
        _emit_outcome(
            runner_id,
            _KeepAliveOutcome.SUBMISSION_FAILED,
            error_type="cancelled",
            queue_delay_s=0.0,
        )


def _run_keepalive_job(runner_id: str, queued_at: float) -> None:
    """Stamp the actual worker start, then resolve the runner's sandbox."""
    started_at = time.monotonic()
    queue_delay_s = max(0.0, started_at - queued_at)
    with _state_lock:
        _retry_after.pop(runner_id, None)
        _last_kept[runner_id] = started_at
        should_prune = max(len(_last_kept), len(_retry_after)) > _THROTTLE_MAX_ENTRIES
    if should_prune:
        _prune_throttle(started_at)
    token = _queue_delay_s.set(queue_delay_s)
    try:
        _keep_alive_for_runner(runner_id)
    finally:
        _queue_delay_s.reset(token)
        with _state_lock:
            _inflight.discard(runner_id)


def _bounded_error_type(exc: BaseException) -> str:
    """Return exception class evidence without carrying exception details."""
    name = type(exc).__name__
    if name and len(name) <= 64 and name.isidentifier():
        return name
    return "Exception"


def _emit_outcome(
    runner_id: str,
    outcome: _KeepAliveOutcome,
    *,
    host_id: str | None = None,
    provider: str | None = None,
    sandbox_id: str | None = None,
    provider_duration_s: float | None = None,
    queue_delay_s: float | None = None,
    error_type: str | None = None,
    message: str | None = None,
) -> None:
    """Emit one bounded, identifier-only record for one refresh attempt."""
    if outcome == _KeepAliveOutcome.EXTENDED:
        level = logging.INFO
    elif outcome in {
        _KeepAliveOutcome.SOFT_FAILED,
        _KeepAliveOutcome.PROVIDER_ERROR,
        _KeepAliveOutcome.RESOLUTION_ERROR,
        _KeepAliveOutcome.SUBMISSION_FAILED,
    }:
        level = logging.WARNING
    else:
        level = logging.DEBUG
    extra = debug_event(
        "managed_keepalive",
        runner_id=runner_id,
        host_id=host_id,
        provider=provider,
        sandbox_id=sandbox_id,
        outcome=outcome.value,
        error_type=error_type,
        provider_duration_s=(
            round(max(0.0, provider_duration_s), 3) if provider_duration_s is not None else None
        ),
        queue_delay_s=(round(max(0.0, queue_delay_s), 3) if queue_delay_s is not None else None),
    )
    if message is not None:
        _logger.log(level, message, extra=extra)
    else:
        _logger.log(
            level,
            "managed sandbox keepalive outcome=%s runner=%s host=%s provider=%s",
            outcome.value,
            runner_id,
            host_id or "unknown",
            provider or "unknown",
            extra=extra,
        )


def _keep_alive_for_runner(runner_id: str) -> None:
    """Resolve *runner_id* to its managed sandbox and extend it. Never raises."""
    queue_delay_s = _queue_delay_s.get()
    try:
        conversation_store, host_store, deployment = (
            _conversation_store,
            _host_store,
            _sandbox_config,
        )
        if conversation_store is None or host_store is None or deployment is None:
            return
        host_ids = {
            conv.host_id
            for conv in conversation_store.list_conversations_by_runner_id(runner_id)
            if conv.host_id
        }
        for host_id in host_ids:
            try:
                host = host_store.get_host(host_id)
            except Exception as exc:  # noqa: BLE001 - one host must not block others
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.RESOLUTION_ERROR,
                    host_id=host_id,
                    error_type=_bounded_error_type(exc),
                    queue_delay_s=queue_delay_s,
                )
                continue
            # Only a server-provisioned sandbox has one to extend; a CLI host has
            # no sandbox_id / provider and is left alone.
            if host is None:
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.NO_HOST,
                    host_id=host_id,
                    queue_delay_s=queue_delay_s,
                )
                continue
            if not host.sandbox_id or not host.sandbox_provider:
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.NO_SANDBOX,
                    host_id=host_id,
                    provider=host.sandbox_provider,
                    queue_delay_s=queue_delay_s,
                )
                continue
            # for_provider, NOT recorded: recorded() falls back to the deployment
            # default when the host's provider is no longer offered, which is safe
            # only for callers that then compare launcher.provider against the row
            # (see _launcher_for_teardown). Extending is best-effort with nothing
            # to fall back to, so a config for some OTHER provider would push a
            # deadline on the wrong backend using a foreign sandbox id. Skip.
            try:
                config = deployment.for_provider(host.sandbox_provider)
            except Exception as exc:  # noqa: BLE001 - one provider must not block others
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.PROVIDER_ERROR,
                    host_id=host_id,
                    provider=host.sandbox_provider,
                    sandbox_id=host.sandbox_id,
                    error_type=_bounded_error_type(exc),
                    queue_delay_s=queue_delay_s,
                )
                continue
            if config is None:
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.PROVIDER_UNAVAILABLE,
                    host_id=host_id,
                    provider=host.sandbox_provider,
                    sandbox_id=host.sandbox_id,
                    queue_delay_s=queue_delay_s,
                )
                continue
            # Cache the provider's cadence so the loop sleep and throttle settle
            # onto it (agent_sandbox stays fast; others fall back to the default).
            with _state_lock:
                _runner_interval_s[runner_id] = resolve_managed_keepalive_interval_s(
                    host.sandbox_provider
                )
            provider_started = time.monotonic()
            try:
                extended = config.launcher_factory().keep_alive(host.sandbox_id)
                # INFO from the server layer so the keepalive is visible in the
                # server log (onboarding-layer loggers do not surface there); the
                # provider logs the new deadline at debug. A provider returns
                # False when it attempted but could not confirm the extension (and
                # logged its own warning); the structured outcome below remains
                # explicit about the soft failure.
                outcome = (
                    _KeepAliveOutcome.SOFT_FAILED
                    if extended is False
                    else _KeepAliveOutcome.EXTENDED
                )
                provider_duration_s = time.monotonic() - provider_started
                _emit_outcome(
                    runner_id,
                    outcome,
                    host_id=host_id,
                    provider=host.sandbox_provider,
                    sandbox_id=host.sandbox_id,
                    provider_duration_s=provider_duration_s,
                    error_type=("soft_failure" if extended is False else None),
                    message=(
                        f"kept managed sandbox {host.sandbox_id} alive "
                        f"(provider {host.sandbox_provider})"
                        if extended is not False
                        else None
                    ),
                    queue_delay_s=queue_delay_s,
                )
            except SandboxCapabilityError as exc:
                # Provider cannot extend a sandbox (e.g. kubernetes): today's
                # behaviour, nothing to log every 10 minutes.
                provider_duration_s = time.monotonic() - provider_started
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.UNSUPPORTED,
                    host_id=host_id,
                    provider=host.sandbox_provider,
                    sandbox_id=host.sandbox_id,
                    provider_duration_s=provider_duration_s,
                    error_type=_bounded_error_type(exc),
                    queue_delay_s=queue_delay_s,
                )
            except Exception as exc:  # noqa: BLE001 - one provider must not block others
                provider_duration_s = time.monotonic() - provider_started
                _emit_outcome(
                    runner_id,
                    _KeepAliveOutcome.PROVIDER_ERROR,
                    host_id=host_id,
                    provider=host.sandbox_provider,
                    sandbox_id=host.sandbox_id,
                    provider_duration_s=provider_duration_s,
                    error_type=_bounded_error_type(exc),
                    queue_delay_s=queue_delay_s,
                )
    # Keepalive is best effort: it must never disrupt the runner tunnel.
    except Exception as exc:  # noqa: BLE001
        _emit_outcome(
            runner_id,
            _KeepAliveOutcome.RESOLUTION_ERROR,
            error_type=_bounded_error_type(exc),
            queue_delay_s=queue_delay_s,
        )
