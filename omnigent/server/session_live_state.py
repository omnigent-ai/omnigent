"""Best-effort persistence of per-session live state to the conversations table.

The sidebar's live fields — ``runner_online``, turn ``status``, and the
pending-approval count — were historically served from in-memory caches
that exist only on the server replica holding a session's runner tunnel
(the tunnel registry, the SSE-relay status cache, and the
pending-elicitations index). Under host_id replica sharding a session
list / ``WS /v1/sessions/updates`` request can land on any replica, so
those fields must also live somewhere every replica can read: the
``conversations`` row (regional DB).

This module is the single write chokepoint. The in-memory caches remain
the synchronous source on the tunnel-holding replica; every cache write
also enqueues a row write here. Writes are:

- **best-effort** — a failed write logs and is dropped; live state is
  display state, and the next transition rewrites it. A dropped write
  also evicts its dedupe entry, so the next *identical* publish is not
  swallowed and gets a fresh attempt (see :func:`_submit`).
- **ordered** — a single-worker executor serializes writes, so a
  ``running`` → ``idle`` pair can never apply out of order.
- **off the event loop** — the store is synchronous SQLAlchemy; callers
  (the SSE relay, the tunnel handlers, the pub-sub hot path) only pay a
  dict check and a queue put. The write runs in a copy of the caller's
  ``contextvars`` (see :func:`_submit`) so the per-request
  ``workspace_scope`` — which every store query filters on — reaches the
  worker thread; a bare executor would run at the default workspace and
  every ``WHERE workspace_id == …`` would match no rows on a multi-tenant
  replica.
- **deduplicated** — re-publishing an unchanged status / count is a
  no-op, so chatty relays don't turn into row churn.

No-op until :func:`configure` wires a store (the server app does this at
startup); the runner process and unit tests that never configure it are
unaffected.
"""

from __future__ import annotations

import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from omnigent.db.enum_codecs import SESSION_LIVE_STATUS

if TYPE_CHECKING:
    from omnigent.stores import ConversationStore
    from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# Statuses the live-status codec can encode. Derived from the codec's own
# map so the two never drift. ``SessionStatusEvent.status`` additionally
# permits ``"launching"`` (runner-local sub-agent bookkeeping that never
# rides as an external ``session.status`` today), which the codec can't
# encode — see ``persist_live_status``.
_KNOWN_LIVE_STATUSES: frozenset[str] = frozenset(SESSION_LIVE_STATUS)

_store: ConversationStore | None = None
# Scheduled-task store for the event-driven run-completion hook. Wired
# alongside ``_store`` by :func:`configure`; ``None`` disables the hook (the
# runner process and unit tests that never configure it are unaffected).
_scheduled_task_store: ScheduledTaskStore | None = None
# Single worker => writes apply in submission order (see module docstring).
_executor: ThreadPoolExecutor | None = None
# Last status seen per session, for dedupe — the value whose write was
# enqueued, or (for an unencodable status) the value whose warning was
# already logged, so repeats of either are suppressed. Unbounded like the
# in-memory caches these writes mirror; entries live for the process.
_last_status: dict[str, str] = {}
# Last count persisted per session, for dedupe.
_last_pending: dict[str, int] = {}


def configure(
    store: ConversationStore | None,
    scheduled_task_store: ScheduledTaskStore | None = None,
) -> None:
    """
    Wire (or clear) the stores live-state writes go to.

    :param store: The server's conversation store, or ``None`` to
        disable persistence (tests / non-server processes).
    :param scheduled_task_store: The server's scheduled-task store, enabling
        the event-driven run-completion hook
        (:func:`persist_scheduled_run_completion`); ``None`` disables it.
    """
    global _store, _scheduled_task_store
    _store = store
    _scheduled_task_store = scheduled_task_store
    _last_status.clear()
    _last_pending.clear()


def _submit(description: str, fn, *args, on_failure=None) -> None:  # type: ignore[no-untyped-def]
    """
    Run one store write on the ordered background worker.

    The write runs inside a snapshot of the *caller's* ``contextvars``
    (``copy_context().run``). The store filters every query on
    ``current_workspace_id()``, a ``ContextVar`` the multi-tenant request
    middleware binds per request via ``workspace_scope``; a bare
    ``ThreadPoolExecutor.submit`` would run the write at the default
    workspace (0), so on a multi-tenant replica every
    ``UPDATE ... WHERE workspace_id == …`` would match no rows and the
    whole cross-replica mirror would silently no-op. Copying the context
    is the same thing ``asyncio.to_thread`` (used on the read path) does.

    :param description: Log label on failure, e.g. ``"live_status"``.
    :param fn: The store method to call.
    :param args: Arguments for *fn*.
    :param on_failure: Optional zero-arg callback run (on the worker
        thread) when the write raises. Used to evict a dedupe entry so a
        dropped write's value can be re-attempted by the next identical
        publish instead of being swallowed.
    """
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-live-state")

    ctx = contextvars.copy_context()

    def _run() -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — best-effort display state
            _logger.warning("session live-state write failed (%s)", description, exc_info=True)
            if on_failure is not None:
                on_failure()

    _executor.submit(ctx.run, _run)


def persist_live_status(session_id: str, status: str) -> None:
    """
    Persist a relay-observed turn status transition.

    Called wherever ``_session_status_cache`` is written. Deduplicated:
    only an actual transition reaches the database.

    :param session_id: Session/conversation identifier.
    :param status: One of ``idle`` / ``running`` / ``waiting`` / ``failed``.
    """
    if _store is None:
        return
    if status not in _KNOWN_LIVE_STATUSES:
        # ``SessionStatusEvent.status`` permits values the live-status codec
        # can't encode (``"launching"``), and the relay forwards raw event
        # statuses. Drop unknown values here rather than at the store: the
        # encode would raise, and the best-effort ``_evict`` on that failure
        # would clear the dedupe entry, so every republish would re-attempt
        # and re-log. Warn once (this transition is deduped away) and skip.
        if _last_status.get(session_id) != status:
            _logger.warning(
                "session live-state: skipping unencodable status %r for %s",
                status,
                session_id,
            )
        _last_status[session_id] = status
        return
    if _last_status.get(session_id) == status:
        return
    _last_status[session_id] = status

    def _evict() -> None:
        # A dropped write must not leave the dedupe cache asserting this
        # value reached the DB — otherwise a later identical publish is
        # swallowed and the row stays stale until a *different* status
        # arrives. Evict only if we still own the entry (a newer publish
        # may have overwritten it, and its write is the live one).
        if _last_status.get(session_id) == status:
            _last_status.pop(session_id, None)

    _submit("live_status", _store.set_session_live_status, session_id, status, on_failure=_evict)


def persist_scheduled_run_completion(
    conversation_id: str,
    run_status: str,
    *,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    """Transition a scheduled-task run to terminal when its turn ends.

    The event-driven completion mechanism: called from ``_publish_status``
    wherever a session reaches a durable terminal edge (``idle`` = the turn
    completed, ``failed`` = it errored/disconnected). Most conversations are
    not scheduled-task fires, so the reverse lookup returns ``None`` and this
    is a cheap no-op; only a fired conversation with a still-``running`` run
    gets transitioned.

    Runs on the SAME ordered single-worker executor as
    :func:`persist_live_status`, inside a copy of the caller's ``contextvars``
    (see :func:`_submit`). This is load-bearing: the store filters every query
    on ``current_workspace_id()``, and the reverse lookup + ``update_run`` must
    resolve to the fired run's workspace — the relay call site's
    ``workspace_scope`` reaches the worker thread exactly as it does for the
    ``live_status`` mirror. A bare executor would run at workspace 0 and match
    no rows on a multi-tenant replica.

    Idempotent by construction: ``update_run`` is conditional on
    ``WHERE status = running``, so a run already terminal (a fire-time
    ``skipped``/``failed``, or the startup/lazy backstop) is never clobbered
    and a terminal edge seen twice transitions at most once. Best-effort like
    the other writes here — a failure logs and is dropped; the backstop
    (startup sweep / lazy-on-read) is the durability guarantee for the rare
    dropped-write or restart-in-flight case.

    :param conversation_id: The fired conversation whose turn just ended.
    :param run_status: Terminal run status to set — ``"succeeded"`` (turn
        completed) or ``"failed"`` (turn errored/cancelled/disconnected).
    :param error_code: Short failure classification when ``run_status`` is
        ``"failed"`` (e.g. the conversation's ``last_task_error_code``).
    :param error: Optional human-readable failure detail for ``"failed"``.
    """
    store = _scheduled_task_store
    if store is None:
        return

    def _transition() -> None:
        run = store.get_running_run_by_conversation(conversation_id)
        if run is None:
            # Not a scheduled fire, or its run is already terminal — nothing to
            # do. This is the common case (interactive sessions).
            return
        store.update_run(
            run.id,
            status=run_status,
            finished_at=int(time.time()),
            error=error,
            error_code=error_code,
        )

    _submit("scheduled_run_completion", _transition)


def persist_pending_count(conversation_id: str, count: int) -> None:
    """
    Persist an outstanding-elicitation count change.

    Wired as :func:`omnigent.runtime.pending_elicitations`'s persist
    hook; runs on the pub-sub hot path, so it must stay cheap.

    :param conversation_id: Session/conversation identifier.
    :param count: Outstanding elicitations, ``>= 0``.
    """
    if _store is None or _last_pending.get(conversation_id) == count:
        return
    _last_pending[conversation_id] = count

    def _evict() -> None:
        # See persist_live_status._evict: keep the dedupe cache honest so a
        # dropped count write can be re-attempted by the next publish.
        if _last_pending.get(conversation_id) == count:
            _last_pending.pop(conversation_id, None)

    _submit(
        "pending_count",
        _store.set_pending_elicitation_count,
        conversation_id,
        count,
        on_failure=_evict,
    )


def touch_runner_liveness(runner_ids: list[str]) -> None:
    """
    Stamp ``runner_last_seen`` (now) for sessions bound to live runners.

    Called on the tunnel-holding replica: once on runner-tunnel connect,
    then every ping interval from that tunnel's own ping loop
    (``runner_tunnel._ping_loop``). Re-stamping from the per-connection
    ping loop — rather than a central lifespan sweep over the whole
    registry — keeps the write inside the tunnel handler's
    ``workspace_scope``, so the row's ``workspace_id`` filter resolves to
    the owning workspace on a multi-tenant replica. It mirrors how the
    host tunnel refreshes ``host_store.heartbeat`` from its ping loop.

    :param runner_ids: Runner ids with a live tunnel. Empty = no-op.
    """
    if _store is None or not runner_ids:
        return
    _submit("runner_liveness", _store.touch_runner_liveness, list(runner_ids), int(time.time()))


def clear_runner_liveness(runner_id: str) -> None:
    """
    Clear ``runner_last_seen`` for a gracefully-disconnected runner.

    Flips the sidebar offline immediately instead of waiting out the
    freshness TTL. An ungraceful death (host / replica crash) never
    reaches this — the TTL self-corrects it.

    :param runner_id: The disconnected runner's id.
    """
    if _store is None:
        return
    _submit("runner_liveness_clear", _store.clear_runner_liveness, runner_id)
