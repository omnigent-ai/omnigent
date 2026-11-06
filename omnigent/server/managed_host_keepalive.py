"""Keep a managed sandbox warm while its agent is still running.

A runner reports on each tunnel pong whether it did work since its previous
pong (``PongFrame.busy``, decided by
:class:`~omnigent.runner.busy_check.RunnerBusyCheck`). A live tunnel alone is not
enough: viewing a session keeps a runner connected without any work. So a
reporting runner is refreshed only while it was busy within its window: one
refresh interval plus two pong intervals, or its provider's keep-warm grace if
longer (``keep_warm_after_work_s``, by default the runner idle timeout,
so a sandbox stays warm as long as an idle runner would stay alive). A busy
report after the window ran out refreshes at once, because the platform's idle
timer has been running since the last refresh. Work the server sends to the
runner (a session event) counts as a busy report right away. Nothing is
refreshed before a runner's first pong says whether it reports, so reconnecting
to a replica without its history cannot extend an idle sandbox. A runner that
never reports (an older runner) keeps the previous behaviour: refreshed for as
long as its tunnel is up, from its first pong.

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

import contextvars
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from omnigent.onboarding.sandboxes.base import (
    SandboxCapabilityError,
    resolve_managed_keepalive_interval_s,
)
from omnigent.runner.busy_check import DEFAULT_RUNNER_IDLE_TIMEOUT_S

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

# Seconds between runner pongs (runner_tunnel.PING_INTERVAL_S; not imported, as
# that module imports this one). The window adds two of them, so a runner still
# working at its last pong is refreshed on every tick until the next one.
_PONG_INTERVAL_S = 30.0

# runner_id -> monotonic seconds of its last work: a busy pong, dispatched work,
# or the idle time an idle pong reports.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_last_busy_at: dict[str, float] = {}
# runner_id -> its provider's keep_warm_after_work_s, cached with the cadence.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_runner_warm_grace_s: dict[str, float] = {}
# runner_id -> monotonic seconds of its latest busy report, busy or idle. Marks a
# runner as reporting; an idle one is no longer refreshed, so pruning reads it too.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_last_report_at: dict[str, float] = {}
# Runners whose pongs carry no busy report (older builds): refreshed for as long
# as their tunnel is up. A runner in neither this set nor _last_report_at has not
# answered a ping yet, and is not refreshed until it does: refreshing it on
# connect would let every reconnect to a fresh replica extend an idle sandbox.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by runner_id
_unreporting_runners: set[str] = set()


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


def is_enabled() -> bool:
    """Whether managed sandboxes are configured, so the keepalive is active."""
    return _sandbox_config is not None and _host_store is not None


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


def record_pong(runner_id: str, busy: bool | None, *, idle_s: float | None = None) -> None:
    """Note whether *runner_id* is doing work, from one of its tunnel pongs.

    Once a runner reports, :func:`touch` refreshes it only while it was busy
    within its window (see :func:`_recently_busy`). A busy report after that
    window ran out refreshes right away: the platform's idle timer has been
    running since the last refresh, so new work must not wait for the next tick.

    :param runner_id: Runner that sent the pong.
    :param busy: The pong's busy report; ``None`` when the runner does not report.
    :param idle_s: Seconds since the runner last did work, as it reports. Lets a
        server without this runner's history (a restart, or a reconnect to
        another replica) resume its keep-warm grace; it is never new work.
    """
    if _sandbox_config is None or _host_store is None:
        return
    # Refreshes from here are forced: they happen between the keepalive loop's
    # ticks and must not move its throttle clock, or the next tick is skipped.
    if busy is None:
        # An older runner build: refresh for its tunnel's lifetime, starting now.
        if runner_id not in _unreporting_runners:
            forget_reports(runner_id)
            _unreporting_runners.add(runner_id)
            touch(runner_id, force=True)
        return
    _unreporting_runners.discard(runner_id)
    now = time.monotonic()
    first_report = runner_id not in _last_report_at
    _last_report_at[runner_id] = now
    if first_report and len(_last_report_at) > _THROTTLE_MAX_ENTRIES:
        # Idle runners report without being refreshed, so prune from here too.
        _prune_throttle(now)
    if busy:
        resumed = first_report or not _recently_busy(runner_id, now)
        _last_busy_at[runner_id] = now
        if resumed:
            touch(runner_id, force=True)
        return
    if idle_s is not None:
        last_work = now - idle_s
    elif runner_id not in _last_busy_at:
        # No idle time reported: count from now rather than never.
        last_work = now
    else:
        last_work = None
    if last_work is not None:
        _last_busy_at[runner_id] = max(_last_busy_at.get(runner_id, last_work), last_work)
    if first_report:
        # Refreshes only if the runner is still within its window (a fresh
        # replica resuming the grace); an expired one is left to stop.
        touch(runner_id, force=True)


def forget_reports(runner_id: str) -> None:
    """Drop *runner_id*'s busy-report state, making it a never-reporting runner.

    Called when its pongs stop carrying ``busy`` and when its throttle entry is
    pruned. Not on reconnect: a reporting runner that reconnects while idle must
    stay idle, or every tunnel reconnect would refresh its sandbox.

    :param runner_id: Runner whose reports to drop.
    """
    for state in (_last_busy_at, _last_report_at):
        state.pop(runner_id, None)
    _unreporting_runners.discard(runner_id)


def record_work_dispatched(runner_id: str) -> None:
    """Treat work the server just sent to *runner_id* as a busy report.

    Pongs sample busy only every ping interval, so a message sent right after an
    idle pong would otherwise wait up to that long for its refresh, which can be
    past a short platform deadline. It counts as work at this moment, so the
    runner's window (and keep-warm grace) runs from here.

    :param runner_id: Runner the server forwarded a session event to.
    """
    if runner_id in _unreporting_runners:
        return  # already refreshed for its tunnel's lifetime
    record_pong(runner_id, True)


def touch(runner_id: str, *, force: bool = False) -> None:
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
    :param force: Skip the interval throttle (a refresh from
        :func:`record_pong`, between ticks). A forced refresh leaves the periodic
        schedule alone, so the next tick still refreshes on time.
    """
    if _sandbox_config is None or _host_store is None or _executor is None:
        return
    now = time.monotonic()
    last = _last_kept.get(runner_id)
    if not force and last is not None and now - last < _interval_for(runner_id):
        return
    if runner_id not in _unreporting_runners:
        # Wait for the runner's first pong to say whether it reports; then leave
        # one that has not worked within its window to the platform's idle stop.
        # Until its provider's grace is known, the worker resolves it and decides.
        if runner_id not in _last_report_at:
            return
        if runner_id in _runner_warm_grace_s and not _recently_busy(runner_id, now):
            return
    with _inflight_lock:
        if runner_id in _inflight:
            # Previous attempt for this runner has not finished; skip rather than
            # queue a duplicate. Deliberately leaves _last_kept untouched so the
            # next tick retries as soon as the in-flight one clears.
            return
        _inflight.add(runner_id)
    if not force:
        _last_kept[runner_id] = now
    if len(_last_kept) > _THROTTLE_MAX_ENTRIES:
        _prune_throttle(now)
    ctx = contextvars.copy_context()
    _executor.submit(ctx.run, _keep_alive_for_runner, runner_id)


def _recently_busy(runner_id: str, now: float) -> bool:
    """Whether *runner_id* did work within its keepalive window.

    The window is one refresh interval (so work anywhere since the last tick
    counts) plus two pong intervals (so ticks between pongs keep refreshing a
    runner busy at its last one), or the provider's keep-warm grace if longer.
    """
    grace = _runner_warm_grace_s.get(runner_id, 0.0)
    if math.isinf(grace):
        # The runner never idles out (runner.idle_timeout_s 0): warm while connected.
        return True
    last_busy = _last_busy_at.get(runner_id)
    if last_busy is None:
        return False
    window = max(_interval_for(runner_id) + 2 * _PONG_INTERVAL_S, grace)
    return now - last_busy < window


def _prune_throttle(now: float) -> None:
    """Drop throttle entries older than two slow intervals (their runners are gone)."""
    cutoff = now - 2 * resolve_managed_keepalive_interval_s()
    # A connected idle runner keeps reporting but is no longer refreshed, so a
    # runner is stale only when neither its refresh nor its reports are recent.
    stale = [
        rid
        for rid in set(_last_kept) | set(_last_report_at)
        if max(_last_kept.get(rid, float("-inf")), _last_report_at.get(rid, float("-inf")))
        < cutoff
    ]
    for runner_id in stale:
        _last_kept.pop(runner_id, None)
        _runner_interval_s.pop(runner_id, None)
        _runner_warm_grace_s.pop(runner_id, None)
        forget_reports(runner_id)


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
            # getattr: embedding deployments may build duck-typed configs.
            _runner_warm_grace_s[runner_id] = float(
                getattr(config, "keep_warm_after_work_s", DEFAULT_RUNNER_IDLE_TIMEOUT_S)
            )
            # touch() lets a reporting runner through before its grace is known
            # (a fresh replica); decide now, rather than renew to learn the grace.
            if runner_id in _last_report_at and not _recently_busy(runner_id, time.monotonic()):
                _logger.debug(
                    "runner %s idle past its window; not extending sandbox %s",
                    runner_id,
                    host.sandbox_id,
                )
                continue
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
