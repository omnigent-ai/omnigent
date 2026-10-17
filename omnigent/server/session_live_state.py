"""Ordered persistence of per-session live state to the conversations table.

The sidebar's live fields — ``runner_online``, turn ``status``, and the
pending-approval count — were historically served from in-memory caches
that exist only on the server replica holding a session's runner tunnel
(the tunnel registry, the SSE-relay status cache, and the
pending-elicitations index). Under host_id replica sharding a session
list / ``WS /v1/sessions/updates`` request can land on any replica, so
those fields must also live somewhere every replica can read: the
``conversations`` row (regional DB).

This module is the single write chokepoint. Versioned status transitions
commit before updating the local caches or stream. Writes are:

- **bounded** — status observations retry the same captured owner and payload
  before exposing an exhausted exception to callbacks. Other display writes
  remain best-effort and evict their dedupe entry after failure.
- **ordered** — a single-worker executor serializes writes, so a
  ``running`` → ``idle`` pair can never apply out of order.
- **off the event loop** — the store is synchronous SQLAlchemy; callers
  (the SSE relay, the tunnel handlers, the pub-sub hot path) only pay a
  dict check and a queue put. The write runs in a copy of the caller's
  ``contextvars`` (see :func:`submit`) so the per-request
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
import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.db.enum_codecs import SESSION_LIVE_STATUS
from omnigent.db.workspace_cache import WorkspaceScopedCache, WorkspaceScopedSet
from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.stores import ConversationStore
    from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# Statuses the live-status codec can encode. Derived from the codec's own
# map so the two never drift. ``SessionStatusEvent.status`` additionally
# permits ``"launching"`` (runner-local sub-agent bookkeeping that never
# rides as an external ``session.status`` today), which the codec can't
# encode — see ``persist_live_status``.
_KNOWN_LIVE_STATUSES: frozenset[str] = frozenset(SESSION_LIVE_STATUS)
_OBSERVATION_RETRY_DELAYS_S = (0.1, 0.25)

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
_last_status: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


@dataclass(frozen=True)
class PendingObservation:
    owner: SessionObservation | None
    status: str
    response_id: str | None
    error_key: str | None
    error_labels: dict[str, str] | None
    observation_id: str
    future: Future[SessionObservation | None]
    starts_new_turn: bool = False


_pending_observations: WorkspaceScopedCache[str, PendingObservation] = WorkspaceScopedCache()
_status_error_labels: WorkspaceScopedCache[str, tuple[SessionObservation, dict[str, str]]] = (
    WorkspaceScopedCache()
)
_observed_failures: WorkspaceScopedSet[tuple[str, str]] = WorkspaceScopedSet()
# Last count persisted per session, for dedupe.
_last_pending: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()
# This process's own last touch_runner_liveness() stamp per runner id.
# Never erased by clear_runner_liveness — only configure() resets it.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally-unique runner_id
_last_liveness_stamp: dict[str, int] = {}


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
    from omnigent.runtime import session_stream

    _store = store
    _scheduled_task_store = scheduled_task_store
    _last_status.clear()
    _pending_observations.clear()
    _status_error_labels.clear()
    _observed_failures.clear()
    _last_pending.clear()
    _last_liveness_stamp.clear()
    session_stream.reset_observation_floors()


def conversation_store() -> ConversationStore | None:
    """
    Return the conversation store wired by :func:`configure`.

    :returns: The server's conversation store, or ``None`` when live-state
        persistence is disabled (tests / non-server processes).
    """
    return _store


def submit(
    description: str, fn, *args, on_failure=None, propagate_errors: bool = False
) -> Future[Any]:  # type: ignore[no-untyped-def]
    """
    Run one store-backed task on the ordered background worker.

    The task runs inside a snapshot of the *caller's* ``contextvars``
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
    :param propagate_errors: Preserve an exhausted write exception for callers that
        must return a retryable error; ordinary display tasks remain best-effort.
    :returns: Future containing the result, or ``None`` after a suppressed write failure.
    """
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-live-state")

    ctx = contextvars.copy_context()

    def _run() -> Any:
        try:
            return fn(*args)
        except Exception:
            _logger.warning("session live-state write failed (%s)", description, exc_info=True)
            if on_failure is not None:
                on_failure()
            if propagate_errors:
                raise

    return _executor.submit(ctx.run, _run)


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

    submit("live_status", _store.set_session_live_status, session_id, status, on_failure=_evict)


def forget_live_status(session_id: str) -> None:
    """Drop this process's dedupe state for a session handed to another replica."""
    _last_status.pop(session_id, None)
    _pending_observations.pop(session_id, None)
    _status_error_labels.pop(session_id, None)


def stage_status_error_labels(
    session_id: str, owner: SessionObservation, labels: dict[str, str]
) -> None:
    """Keep enrichment with its captured owner until the status commits."""
    _status_error_labels[session_id] = (owner, labels)


def status_error_labels(
    session_id: str, owner: SessionObservation | None, defaults: dict[str, str]
) -> dict[str, str]:
    """Use enrichment only for the same owner and error, including clears."""
    staged = _status_error_labels.get(session_id)
    if staged is None or staged[0] != owner:
        return defaults
    labels = staged[1]
    keys = ("omnigent.last_task_error_code", "omnigent.last_task_error_message")
    return labels if all(labels.get(key) == defaults.get(key) for key in keys) else defaults


def pending_observation(
    session_id: str, owner: SessionObservation | None = None
) -> PendingObservation | None:
    """Capture an exact queued edge for the same runner binding before awaiting."""
    pending = _pending_observations.get(session_id)
    if (
        pending is not None
        and pending.owner is not None
        and owner is not None
        and (
            pending.owner.runner_id != owner.runner_id
            or pending.owner.binding_epoch != owner.binding_epoch
        )
    ):
        return None
    return pending


def resolved_observation(pending: PendingObservation) -> SessionObservation | None:
    """A failed predecessor supplies no new identity; callers retain their captured fence."""
    try:
        return pending.future.result()
    except Exception:  # noqa: BLE001 — persistence already logged the failed immutable write
        return None


def captured_response_id(
    status: str,
    owner: SessionObservation | None,
    predecessor: PendingObservation | None,
) -> str | None:
    """Retain a captured busy turn without reopening a prior terminal turn."""
    if owner is None:
        return None
    previous_status, response_id = owner.status, owner.response_id
    if (
        predecessor is not None
        and predecessor.owner is not None
        and (
            predecessor.owner.runner_id == owner.runner_id
            and predecessor.owner.binding_epoch == owner.binding_epoch
        )
    ):
        completed = predecessor.future.done()
        queued = resolved_observation(predecessor) if completed else None
        if (queued is not None and (predecessor.owner == owner or queued == owner)) or (
            not completed and predecessor.owner == owner
        ):
            previous_status, response_id = predecessor.status, predecessor.response_id
    if previous_status in {"running", "waiting"} or (
        previous_status == status and status in {"idle", "failed"}
    ):
        return response_id
    return None


def reserve_terminal_startup(
    session_id: str,
    owner: SessionObservation,
    predecessor: PendingObservation | None,
) -> Future[SessionObservation | None] | None:
    """Reserve one exact startup before its provider or input can be accepted."""
    store = _store
    if store is None:
        return None
    token = uuid.uuid4().hex

    def _write() -> SessionObservation | None:
        expected = owner
        queued = resolved_observation(predecessor) if predecessor is not None else None
        if (
            queued is not None
            and predecessor is not None
            and predecessor.owner == owner
            and predecessor.starts_new_turn
        ):
            return None
        if (
            queued is not None
            and predecessor is not None
            and (
                queued == owner
                or (
                    predecessor.owner == owner
                    and not predecessor.starts_new_turn
                    and predecessor.status == queued.status == "idle"
                    and owner.status in {"running", "waiting"}
                    and queued.runner_id == owner.runner_id
                    and queued.binding_epoch == owner.binding_epoch
                    and queued.revision == owner.revision + 1
                    and queued.observation_id == predecessor.observation_id
                    and queued.error_key is None
                    and (
                        owner.response_id is None
                        or queued.response_id in {None, owner.response_id}
                    )
                )
            )
        ):
            expected = queued
        for attempt, delay in enumerate((0.0, *_OBSERVATION_RETRY_DELAYS_S)):
            if delay:
                time.sleep(delay)
            try:
                return store.begin_terminal_startup(session_id, expected, token)
            except Exception:
                if attempt == len(_OBSERVATION_RETRY_DELAYS_S):
                    raise
                _logger.warning(
                    "Retrying terminal startup reservation for %s", session_id, exc_info=True
                )
        return None

    return submit("terminal_startup", _write, propagate_errors=True)


def complete_terminal_startup(
    session_id: str, owner: SessionObservation
) -> Future[SessionObservation | None] | None:
    """Complete the captured reservation without retrying its provider or input."""
    store = _store
    if store is None:
        return None
    token = hashlib.sha256(("terminal-ready:" + owner.model_dump_json()).encode()).hexdigest()

    def _write() -> SessionObservation | None:
        for attempt, delay in enumerate((0.0, *_OBSERVATION_RETRY_DELAYS_S)):
            if delay:
                time.sleep(delay)
            try:
                return store.finish_terminal_startup(session_id, owner, token)
            except Exception:
                if attempt == len(_OBSERVATION_RETRY_DELAYS_S):
                    raise
                _logger.warning(
                    "Retrying terminal startup completion for %s", session_id, exc_info=True
                )
        return None

    return submit("terminal_ready", _write, propagate_errors=True)


def persist_status_observation(
    session_id: str,
    status: str,
    *,
    owner: SessionObservation | None,
    predecessor: PendingObservation | None,
    response_id: str | None,
    error: dict[str, Any] | None,
    error_labels: dict[str, str] | None,
    starts_new_turn: bool = False,
    on_commit: Callable[[SessionObservation, bool], object],
    on_current: Callable[[Conversation], object],
    on_source_failure: Callable[[str], None] | None = None,
) -> Future[SessionObservation | None] | None:
    """Publish only after a captured owner's ordered observation commits."""
    store = _store
    if store is None or status not in _KNOWN_LIVE_STATUSES:
        return None
    previous = _pending_observations.get(session_id)
    explicit_response_id = response_id
    repeats_captured_idle = (
        status == "idle" and response_id is None and owner is not None and owner.status == "idle"
    )
    # An idle resync reasserts only its captured terminal result.
    if repeats_captured_idle and owner is not None:
        response_id = owner.response_id
    # PTY activity carries no turn ID and must not erase a known busy turn.
    if status in {"running", "waiting", "failed"} and response_id is None:
        response_id = captured_response_id(status, owner, predecessor)
    error_key = (
        hashlib.sha256(
            repr((sorted(error.items()), sorted((error_labels or {}).items()))).encode()
        ).hexdigest()
        if error
        else None
    )
    signature = (status, response_id, error_key)
    duplicate_pending = previous is not None and (
        previous.owner == owner
        and previous.starts_new_turn == starts_new_turn
        and (previous.status, previous.response_id, previous.error_key) == signature
    )
    if duplicate_pending:
        assert previous is not None
        observation_id = previous.observation_id
    else:
        observation_id = uuid.uuid4().hex
    if status == "failed" and owner is not None and not duplicate_pending:
        if (
            owner.status == "failed"
            and explicit_response_id is None
            and owner.response_id == response_id
            and owner.error_key == error_key
            and owner.observation_id
        ):
            observation_id = owner.observation_id
        else:
            identity = (
                ("response", response_id)
                if response_id is not None
                else (
                    "observation",
                    owner.revision,
                    owner.observation_id,
                    predecessor.observation_id if predecessor is not None else None,
                )
            )
            observation_id = hashlib.sha256(
                repr(
                    (
                        owner.runner_id,
                        owner.binding_epoch,
                        identity,
                        error_key,
                    )
                ).encode()
            ).hexdigest()
    if on_source_failure is not None and (session_id, observation_id) not in _observed_failures:
        _observed_failures.add((session_id, observation_id))
        on_source_failure(observation_id)

    def _write() -> SessionObservation | None:
        expected = owner
        resolved_predecessor = False
        observation: SessionObservation | None = None
        current: Conversation | None = None
        for attempt, delay in enumerate((0.0, *_OBSERVATION_RETRY_DELAYS_S)):
            if delay:
                time.sleep(delay)
            try:
                # Global user intent resolves once; runner events already captured an owner.
                if expected is None:
                    expected = store.get_session_observation(session_id)
                if expected is None:
                    return None
                if not resolved_predecessor:
                    queued = (
                        resolved_observation(predecessor)
                        if predecessor is not None and not repeats_captured_idle
                        else None
                    )
                    if (
                        queued is not None
                        and predecessor is not None
                        and (predecessor.owner == expected or queued == expected)
                    ):
                        expected = queued
                    resolved_predecessor = True
                observation = store.record_session_observation(
                    session_id,
                    status,
                    response_id=response_id,
                    observation_id=observation_id,
                    error_key=error_key,
                    error_labels=error_labels,
                    expected_observation=expected,
                    expected_binding_epoch=expected.binding_epoch,
                    expected_revision=expected.revision,
                    starts_new_turn=starts_new_turn,
                )
                if (
                    status == "failed"
                    and expected.status == "failed"
                    and response_id == expected.response_id
                    and error_key == expected.error_key
                ):
                    # A new startup can fail with the same last normal payload.
                    token = hashlib.sha256(
                        ("terminal-failed:" + expected.model_dump_json()).encode()
                    ).hexdigest()
                    completed = store.finish_terminal_startup(session_id, expected, token)
                    if completed is not None:
                        observation = completed
                current = store.get_conversation(session_id) if observation is not None else None
            except Exception:
                if attempt == len(_OBSERVATION_RETRY_DELAYS_S):
                    _logger.warning(
                        "Session observation retries exhausted for %s after %d attempts",
                        session_id,
                        attempt + 1,
                    )
                    raise
                _logger.warning(
                    "Retrying session observation for %s after persistence failure",
                    session_id,
                    exc_info=True,
                )
                continue
            break
        if (
            observation is not None
            and current is not None
            and current.session_observation == observation
        ):
            if status == "failed" and observation.observation_id is not None:
                _observed_failures.add((session_id, observation.observation_id))
            on_commit(observation, current.terminal_pending)
            return observation
        current = store.get_conversation(session_id)
        if current is not None:
            on_current(current)
        return None

    future = submit("session_observation", _write, propagate_errors=True)
    _pending_observations[session_id] = PendingObservation(
        owner,
        status,
        response_id,
        error_key,
        dict(error_labels) if error_labels is not None else None,
        observation_id,
        future,
        starts_new_turn,
    )
    return future


def persist_scheduled_run_completion(
    conversation_id: str,
    run_status: str,
    *,
    error_code: str | None = None,
    error: str | None = None,
    observation: SessionObservation | None = None,
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
    (see :func:`submit`). This is load-bearing: the store filters every query
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
        if observation is not None:
            if _store is None or _store.get_session_observation(conversation_id) != observation:
                return
        run = store.get_running_run_by_conversation(conversation_id)
        run_id = run.id if run is not None else None
        if observation is not None and (
            _store is None or _store.get_session_observation(conversation_id) != observation
        ):
            return
        if run_id is None:
            # Not a scheduled fire, or its run is already terminal — nothing to
            # do. This is the common case (interactive sessions).
            return
        store.update_run(
            run_id,
            status=run_status,
            finished_at=int(time.time()),
            error=error,
            error_code=error_code,
        )

    submit("scheduled_run_completion", _transition)


def capture_scheduled_run_id(conversation_id: str) -> str | None:
    """Read the old run while capturing an offline decision on the worker."""
    store = _scheduled_task_store
    if store is None:
        return None
    run = store.get_running_run_by_conversation(conversation_id)
    return run.id if run is not None else None


def persist_owned_scheduled_run_completion(
    conversation_id: str,
    decision: RunnerOfflineDecision,
    *,
    error_code: str | None,
    error: str | None,
) -> None:
    """Settle only the captured run, never a later run for this conversation."""
    store = _scheduled_task_store
    if store is None or _store is None or decision.scheduled_run_id is None:
        return

    def _transition() -> None:
        store.update_run(
            decision.scheduled_run_id,
            status="failed",
            finished_at=int(time.time()),
            error=error,
            error_code=error_code,
        )

    submit(f"owned_scheduled_run_completion {conversation_id}", _transition)


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

    submit(
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

    Also records the stamp in :func:`last_liveness_stamp`, so a later
    disconnect on this same replica can tell its own writes apart from a
    fresher one another replica made after the runner re-tunnelled there.

    :param runner_ids: Runner ids with a live tunnel. Empty = no-op.
    """
    if _store is None or not runner_ids:
        return
    now = int(time.time())
    for runner_id in runner_ids:
        _last_liveness_stamp[runner_id] = now
    submit("runner_liveness", _store.touch_runner_liveness, list(runner_ids), now)


def last_liveness_stamp(runner_id: str) -> int | None:
    """
    Return the last ``runner_last_seen`` value THIS process itself stamped.

    Lets a disconnect handler distinguish a stamp another replica wrote
    after the runner re-tunnelled there from one this replica wrote
    itself, without racing the best-effort write's own executor.

    :param runner_id: Runner id to look up.
    :returns: The epoch-second value from this process's most recent
        :func:`touch_runner_liveness` call naming *runner_id*, or
        ``None`` if it never stamped one.
    """
    return _last_liveness_stamp.get(runner_id)


def clear_runner_liveness(runner_id: str) -> None:
    """
    Clear ``runner_last_seen`` for a gracefully-disconnected runner.

    Flips the sidebar offline immediately instead of waiting out the
    freshness TTL. An ungraceful death (host / replica crash) never
    reaches this — the TTL self-corrects it. Passes this process's own
    last stamp as ``not_after``, so the clear can never erase a fresher
    stamp another replica already wrote for a runner that re-tunnelled
    there before this disconnect was processed.

    :param runner_id: The disconnected runner's id.
    """
    if _store is None:
        return
    submit(
        "runner_liveness_clear",
        _store.clear_runner_liveness,
        runner_id,
        last_liveness_stamp(runner_id),
    )
