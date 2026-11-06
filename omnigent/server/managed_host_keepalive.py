"""Keep a managed sandbox warm while its agent is still running.

A runner reports on each tunnel pong whether it wants its sandbox kept warm
(``PongFrame.keep_warm``, from
:meth:`~omnigent.runner.busy_check.RunnerBusyCheck.keep_warm`: working, or
within its idle timeout of the last work). A live tunnel alone is not enough,
since viewing a session keeps a runner connected without any work. So while a
runner's tunnel is up, :func:`touch` refreshes its sandbox only once the runner
has answered a ping and its latest report does not say ``False``. A runner
whose pongs carry no report (an older build) is refreshed for as long as its
tunnel is up, as before.

This module turns that signal into a provider
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
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from omnigent.onboarding.sandboxes.base import (
    SandboxCapabilityError,
    resolve_managed_keepalive_interval_s,
)

if TYPE_CHECKING:
    from omnigent.server.managed_hosts import ManagedSandboxDeployment
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

# runner_id -> its provider's keepalive cadence (seconds), filled by
# _keep_alive_for_runner once the runner's provider is resolved. Until then the
# fast agent_sandbox cadence is used (see _interval_for) so an agent_sandbox's
# short window is never under-refreshed; a slower provider self-corrects to its
# own cadence after its first keepalive, at the cost of one early refresh.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_runner_interval_s: dict[str, float] = {}

# Cap on the per-runner throttle map before stale entries are pruned. Runners
# are transient, so without this a long-lived server accumulates one dead key
# per session.
# ponytail: prune-on-grow, not a background sweep: swap if profiling says so.
_THROTTLE_MAX_ENTRIES = 4096

_conversation_store: ConversationStore | None = None
_host_store: HostStore | None = None
_sandbox_config: ManagedSandboxDeployment | None = None
_executor: ThreadPoolExecutor | None = None

# runner_id -> monotonic seconds of its last keep_alive attempt.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_last_kept: dict[str, float] = {}

# Runners with work queued or running on the executor. The throttle alone bounds
# the queue only while calls finish inside the interval; this also keeps a stalled
# provider from stacking a second job for the same runner behind the first.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


@dataclass
class KeepWarmReport:
    """The latest keep-warm report on one runner tunnel connection.

    Owned by that connection's keepalive loop (see :func:`open_report`), and
    only read and written on the event loop.
    """

    #: Whether the runner has answered a ping on this connection yet.
    received: bool = False
    #: The latest pong's ``keep_warm``; ``None`` when the runner does not report.
    keep_warm: bool | None = None
    #: Set when a pong makes a refresh due, so the loop need not wait a tick.
    wake: asyncio.Event = field(default_factory=asyncio.Event)


# runner_id -> the report of its current tunnel connection.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_reports: dict[str, KeepWarmReport] = {}


def is_enabled() -> bool:
    """Whether managed sandboxes are configured, so the keepalive is active."""
    return _sandbox_config is not None and _host_store is not None


def open_report(runner_id: str) -> KeepWarmReport:
    """Start tracking keep-warm reports for a new tunnel connection of *runner_id*.

    A reconnect replaces the previous connection's report, so nothing carries
    over: the new connection waits for its own first pong.

    :param runner_id: Runner whose tunnel just connected.
    :returns: The connection's report, for its keepalive loop.
    """
    report = KeepWarmReport()
    _reports[runner_id] = report
    return report


def close_report(runner_id: str, report: KeepWarmReport) -> None:
    """Stop tracking *report* when its tunnel connection ends.

    Leaves a newer connection's report for the same runner alone.

    :param runner_id: Runner whose tunnel connection ended.
    :param report: The report that connection opened.
    """
    if _reports.get(runner_id) is report:
        del _reports[runner_id]


def record_pong(runner_id: str, keep_warm: bool | None) -> None:
    """Record *runner_id*'s keep-warm report from one of its tunnel pongs.

    Wakes the connection's keepalive loop when the pong makes a refresh due:
    the first pong, or one that turns a ``False`` report into a refresh. The
    platform's idle timer has been running since the last refresh, so resumed
    work must not wait for the next tick.

    :param runner_id: Runner that sent the pong.
    :param keep_warm: The pong's ``keep_warm``; ``None`` when not reported.
    """
    report = _reports.get(runner_id)
    if report is None:
        return
    due = not report.received or (report.keep_warm is False and keep_warm is not False)
    report.received = True
    report.keep_warm = keep_warm
    if due:
        report.wake.set()


def _wants_refresh(runner_id: str) -> bool:
    """Whether *runner_id*'s tunnel reports allow a refresh now.

    A runner without a tracked connection (a caller outside the tunnel loop)
    keeps the plain throttled behaviour.
    """
    report = _reports.get(runner_id)
    if report is None:
        return True
    return report.received and report.keep_warm is not False


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
    if sandbox_config is not None and host_store is not None and _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="managed-keepalive")


def _interval_for(runner_id: str) -> float:
    """The keepalive cadence for *runner_id*: its provider's, once cached, else
    the fast agent_sandbox cadence so a short-window sandbox is never under-refreshed."""
    return _runner_interval_s.get(runner_id, resolve_managed_keepalive_interval_s("agent_sandbox"))


def keepalive_interval_s(runner_id: str) -> float:
    """Seconds the runner tunnel's keepalive loop sleeps between refreshes for
    *runner_id* (and the per-runner throttle in :func:`touch`).

    Provider-scoped: agent_sandbox refreshes fast because its window is short;
    other providers keep the cheaper default so they are not over-called. The loop
    sleep and the throttle read the same value, so they cannot disagree.
    """
    return _interval_for(runner_id)


def touch(runner_id: str) -> bool:
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

    Skipped while the runner's tunnel reports say not to (see
    :func:`record_pong`).

    :param runner_id: Runner with a live tunnel, e.g. ``"runner_token_abc"``.
    :returns: Whether a refresh was submitted.
    """
    if _sandbox_config is None or _host_store is None or _executor is None:
        return False
    if not _wants_refresh(runner_id):
        return False
    now = time.monotonic()
    last = _last_kept.get(runner_id)
    if last is not None and now - last < _interval_for(runner_id):
        return False
    with _inflight_lock:
        if runner_id in _inflight:
            # Previous attempt for this runner has not finished; skip rather than
            # queue a duplicate. Deliberately leaves _last_kept untouched so the
            # next tick retries as soon as the in-flight one clears.
            return False
        _inflight.add(runner_id)
    _last_kept[runner_id] = now
    if len(_last_kept) > _THROTTLE_MAX_ENTRIES:
        _prune_throttle(now)
    ctx = contextvars.copy_context()
    _executor.submit(ctx.run, _keep_alive_for_runner, runner_id)
    return True


def _prune_throttle(now: float) -> None:
    """Drop throttle entries older than two slow intervals (their runners are gone)."""
    cutoff = now - 2 * resolve_managed_keepalive_interval_s()
    for runner_id in [rid for rid, seen in _last_kept.items() if seen < cutoff]:
        _last_kept.pop(runner_id, None)
        _runner_interval_s.pop(runner_id, None)


def _keep_alive_for_runner(runner_id: str) -> None:
    """Resolve *runner_id* to its managed sandbox and extend it. Never raises."""
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
            host = host_store.get_host(host_id)
            # Only a server-provisioned sandbox has one to extend; a CLI host has
            # no sandbox_id / provider and is left alone.
            if host is None or not host.sandbox_id or not host.sandbox_provider:
                continue
            # for_provider, NOT recorded: recorded() falls back to the deployment
            # default when the host's provider is no longer offered, which is safe
            # only for callers that then compare launcher.provider against the row
            # (see _launcher_for_teardown). Extending is best-effort with nothing
            # to fall back to, so a config for some OTHER provider would push a
            # deadline on the wrong backend using a foreign sandbox id. Skip.
            config = deployment.for_provider(host.sandbox_provider)
            if config is None:
                _logger.debug(
                    "provider %s no longer offered; skipping sandbox %s",
                    host.sandbox_provider,
                    host.sandbox_id,
                )
                continue
            # Cache the provider's cadence so the loop sleep and throttle settle
            # onto it (agent_sandbox stays fast; others fall back to the default).
            _runner_interval_s[runner_id] = resolve_managed_keepalive_interval_s(
                host.sandbox_provider
            )
            try:
                # Owner-identity launchers (e.g. Databricks) refresh with the
                # host owner's own credential, never a server identity.
                from omnigent.server.managed_hosts import build_owner_launcher

                launcher = build_owner_launcher(config, deployment, host.user_id)
                extended = launcher.keep_alive(host.sandbox_id)
                # INFO from the server layer so the keepalive is visible in the
                # server log (onboarding-layer loggers do not surface there); the
                # provider logs the new deadline at debug. A provider returns
                # False when it attempted but could not confirm the extension (and
                # logged its own warning); skip the success line so the log is not
                # self-contradictory.
                if extended is not False:
                    _logger.info(
                        "kept managed sandbox %s alive (provider %s)",
                        host.sandbox_id,
                        host.sandbox_provider,
                    )
            except SandboxCapabilityError:
                # Provider cannot extend a sandbox (e.g. kubernetes): today's
                # behaviour, nothing to log every 10 minutes.
                _logger.debug(
                    "keep_alive unsupported by provider %s; skipping sandbox %s",
                    host.sandbox_provider,
                    host.sandbox_id,
                )
    # Keepalive is best effort: it must never disrupt the runner tunnel.
    except Exception:
        _logger.warning("managed sandbox keepalive failed for runner %s", runner_id, exc_info=True)
    finally:
        with _inflight_lock:
            _inflight.discard(runner_id)
