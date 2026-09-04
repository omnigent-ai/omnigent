"""In-process index of currently outstanding elicitation requests.

Lets the Omnigent server answer "which sessions have a pending approval
prompt?" without scanning per-session state or persisting elicitation
rows. The index is the *sidebar's* view of pending state — it lives
alongside the underlying parked awaiter (a runner-side Future or a
server-side ``_harness_elicitation_registry`` Future) and shares its
lifecycle: when the Omnigent process dies, both the index and every parked
awaiter die together, so the index cannot diverge from the underlying
state into "phantom" pending rows.

The index is populated automatically by
:func:`omnigent.runtime.session_stream.publish` whenever a
``response.elicitation_request`` event passes through (server-emitted
policy elicitations, the claude-native PermissionRequest hook, and
runner-originated elicitations relayed by ``_relay_runner_stream``
all funnel through that single chokepoint). It is decremented either
by the approval-dispatch path on ``POST /v1/sessions/{id}/events``
when an ``approval`` verdict arrives, or by a
``response.elicitation_resolved`` event from the runner when its own
Future timed out or was cancelled without a UI verdict.

The index also stores the full event payload (not just the id) so
``GET /v1/sessions/{id}`` can replay outstanding prompts into the
UI's chat blocks on cold load — the SSE stream itself has no replay,
so an elicitation emitted before the user opened the chat would
otherwise render as nothing.

Outstanding entries are also mirrored to the ``elicitations`` table when a
store is wired (:func:`set_store` — the server does this at startup; the runner
and unit tests leave it unset and are unaffected). The index stays the read
path; the rows exist so a server restart does not take the parked set with it.
:func:`restore_for` reads them back. Unlike the count mirror on the
conversation row, which is best-effort because a stale count self-corrects on
the next transition, these writes are synchronous and lead the index — losing
one is the very failure the mirror exists to prevent.

Limitations:

* Without a store the index is in-memory only; multi-replica Omnigent deploys
  would each see their own slice. This matches the existing
  ``_harness_elicitation_registry`` constraint — when a shared backplane is
  added for the registry, this index should be wired through the same
  backplane.
* Events emitted before the Omnigent server starts (e.g. between turns,
  with the session_stream having dropped them) are not tracked,
  same as every other AP-server-side in-memory state.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.stores.elicitation_store import ElicitationStore

_logger = logging.getLogger(__name__)

# Longest a prompt can still have something waiting on it. The runner parks an
# ASK for at most ``pending_approvals._DEFAULT_WAIT_SECONDS`` (one day), so a
# restored row older than this has no awaiter left to release and is dropped on
# read rather than shown as answerable.
_MAX_PARK_SECONDS = 86400

# Per-conversation mapping of outstanding elicitation_id → original
# event payload. Storing the full event (not just the id) lets
# ``GET /v1/sessions/{id}`` replay the prompt into the UI on cold
# load. Populated by ``record_publish`` on
# ``response.elicitation_request``; drained by ``resolve`` (called
# directly from the approval-dispatch path, or via ``record_publish``
# when a ``response.elicitation_resolved`` event flows through the
# SSE chokepoint). Empty inner dicts are popped eagerly so
# :func:`count_for` doesn't see stale keys.
_pending: dict[str, dict[str, dict[str, Any]]] = {}
_lock = threading.Lock()

# Serializes a prompt's durable write against the failed-delete retry sweep,
# so a stale queued delete can never erase a re-published prompt's fresh row.
# Separate from ``_lock`` so the hot index path never waits on store I/O.
_persist_lock = threading.Lock()

# Optional observer (``subagent_block_notifier``) run synchronously on every
# tracked event — must be cheap + non-blocking. ``None`` (runner, tests) skips it.
_observer: Callable[[str, dict[str, Any]], None] | None = None


def set_elicitation_observer(
    observer: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """
    Register (or clear) the elicitation-event observer.

    :param observer: Callback invoked as ``observer(conversation_id,
        event)`` for every ``response.elicitation_request`` and
        ``response.elicitation_resolved`` event passing through
        :func:`record_publish`. Pass ``None`` to clear (e.g. test
        teardown). Replaces any previously registered observer.
    :returns: None.
    """
    global _observer
    _observer = observer


# Optional per-session count sink, fired with the new count whenever the
# index changes. The server wires it to persist the count on the
# conversation row so replicas that don't hold this session's runner
# tunnel still show parked approvals. Must be cheap + non-blocking.
_count_persist_hook: Callable[[str, int], None] | None = None


def set_count_persist_hook(hook: Callable[[str, int], None] | None) -> None:
    """
    Register (or clear) the pending-count persist hook.

    :param hook: Callback invoked as ``hook(conversation_id, count)``
        after every index mutation (publish adds, resolve drops), with
        the session's new outstanding count. Pass ``None`` to clear.
    """
    global _count_persist_hook
    _count_persist_hook = hook


def _notify_count_hook(conversation_id: str, count: int) -> None:
    """Fire the count persist hook, if any (read-once, like the observer)."""
    hook = _count_persist_hook
    if hook is not None:
        hook(conversation_id, count)


# Durable mirror of the index. ``None`` (runner, unit tests) keeps the index
# purely in-memory and every store call below a no-op.
_store: ElicitationStore | None = None

# Conversations this process has already asked the store about. Bounds
# :func:`restore_for` to one query per conversation per process, which is what
# lets it run without a precondition — a restore is only ever needed once,
# after which the index is authoritative for the rest of the process's life.
_restored: set[str] = set()

# Conversations whose descendant walk has already been granted once by
# :func:`claim_descendant_restore`. Same one-per-process bound as ``_restored``,
# for the ancestor-snapshot path that must look below itself after a restart.
_descendants_probed: set[str] = set()

# Prompts this process has resolved. :func:`restore_for` must never re-surface
# one: its durable row can outlive the verdict (a delete that failed, or a
# store read racing the delete), and replaying it would ask an answered
# question again — the exact defect the mirror exists to prevent.
_resolved_tombstones: set[tuple[str, str]] = set()

# Deletes that failed and still have a durable row to collect. Retried on
# later store activity so a transient outage does not leave answered prompts
# behind to resurrect on the next restart. Keyed by (workspace_id,
# conversation_id, elicitation_id): the store scopes deletes to the ambient
# workspace, and the retry may fire from another tenant's request.
# Known limitation: this queue (and the tombstones above) is process memory,
# so a restart *during* a store outage that already failed a delete loses the
# claim, and the answered row can resurrect once. Closing that double-fault
# window needs durable tombstones (extra schema); accepted for now.
_failed_deletes: set[tuple[int, str, str]] = set()


def set_store(store: ElicitationStore | None) -> None:
    """
    Wire (or clear) the store outstanding prompts are mirrored to.

    :param store: The server's elicitation store, or ``None`` to keep the
        index in-memory only (the runner process and unit tests).
    """
    global _store
    _store = store
    with _lock:
        _restored.clear()
        _descendants_probed.clear()


def _owns_elicitation(conversation_id: str, event: dict[str, Any]) -> bool:
    """
    Whether this conversation owns the prompt, rather than mirroring it.

    A child's prompt is republished into every ancestor's stream so an
    ancestor chat can render and resolve it, carrying ``target_session_id`` to
    say which session actually owns the parked awaiter. Only the owner may
    persist: the durable row is keyed by elicitation id alone, so letting a
    mirror write would move the child's only row onto whichever ancestor
    published last, and the child would restore nothing.

    The index still tracks mirrors — that is what makes the ancestor's card
    render — but the index is per-conversation and cannot collide this way.

    :param conversation_id: Conversation the event was published on.
    :param event: The ``response.elicitation_request`` payload.
    :returns: ``True`` when *conversation_id* owns the prompt.
    """
    params = event.get("params")
    if not isinstance(params, dict):
        return True
    target = params.get("target_session_id")
    if not isinstance(target, str) or not target:
        return True
    return target == conversation_id


def _persist_add(conversation_id: str, elicitation_id: str, event: dict[str, Any]) -> None:
    """
    Write an outstanding prompt to the store, ahead of indexing it.

    Write-ahead so a crash between the two leaves a row for a prompt the index
    never learned about — recoverable — rather than an indexed prompt with no
    row, which is the loss this mirror exists to prevent.

    Failures log and are dropped: a prompt that cannot be persisted should
    still be raised. The session keeps working; only its restart-survival is
    lost, which is the behaviour before this mirror existed.

    :param conversation_id: Session the prompt was raised on.
    :param elicitation_id: The prompt's correlation id.
    :param event: The ``response.elicitation_request`` payload.
    """
    store = _store
    if store is None:
        return
    from omnigent.db.db_models import current_workspace_id
    from omnigent.entities import Elicitation

    # A re-publish supersedes any failed delete still queued for this id:
    # retrying that delete after the write below would erase the new row and
    # silently cost the fresh prompt its restart survival. The persist lock
    # keeps the supersede-and-write atomic against an in-flight retry sweep,
    # whose stale delete could otherwise land between the two. The claim is
    # dropped only after the write succeeds: a failed write leaves the
    # answered row durable, and only the claim still collects it.
    try:
        with _persist_lock:
            store.put(
                Elicitation(
                    id=elicitation_id,
                    workspace_id=current_workspace_id(),
                    conversation_id=conversation_id,
                    created_at=int(time.time()),
                    event=event,
                )
            )
            with _lock:
                _failed_deletes.discard((current_workspace_id(), conversation_id, elicitation_id))
    except Exception:  # never block raising the prompt
        _logger.warning(
            "Could not persist elicitation %r; it will not survive a restart",
            elicitation_id,
            exc_info=True,
        )
        return
    _retry_failed_deletes(store)


def _persist_remove(conversation_id: str, elicitation_id: str) -> None:
    """
    Drop a resolved prompt from the store, ahead of dropping it from the index.

    Store-first for the same reason :func:`_persist_add` is: a crash between
    the two leaves the index holding a prompt whose row is already gone, so the
    next restart forgets it — correct, since it was answered. The reverse order
    would resurrect an answered prompt and ask the user twice.

    :param conversation_id: Session the prompt was raised on.
    :param elicitation_id: The prompt's correlation id.
    """
    store = _store
    if store is None:
        return
    from omnigent.db.db_models import current_workspace_id

    # Under the persist lock: a workflow thread may be re-publishing this
    # deterministic id for the next turn, and this delete landing after that
    # write would erase the fresh row — the same interleaving the retry sweep
    # guards against, on the far more common direct path.
    try:
        with _persist_lock:
            store.delete(conversation_id, elicitation_id)
    except Exception:  # never block resolving the prompt
        # Remember the orphaned row: a restore must not replay it (the
        # tombstone in :func:`resolve` covers that) and a later successful
        # store call retries the delete so the row does not sit until the
        # next restart resurrects it.
        with _lock:
            _failed_deletes.add((current_workspace_id(), conversation_id, elicitation_id))
        _logger.warning(
            "Could not delete resolved elicitation %r",
            elicitation_id,
            exc_info=True,
        )
        return
    _retry_failed_deletes(store)


def _retry_failed_deletes(store: ElicitationStore) -> None:
    """
    Retry deletes that failed earlier, now that the store answered a call.

    Each entry's claim is re-checked and its delete performed under the
    persist lock, which :func:`_persist_add` holds across its supersede and
    write — so a concurrent re-publish of the same id cannot interleave with a
    stale delete and lose the fresh row's restart survival. The delete also
    runs under the workspace that queued it: the store scopes deletes to the
    ambient tenant, and this sweep may be triggered by another workspace's
    request, where the delete would match nothing while the entry was
    unconditionally dropped — leaving the answered row to resurrect on the
    next restart.

    A delete that raises stops the sweep — the store is presumably down again
    and the remaining entries keep their retry claim. A delete that returns
    ``False`` means the row is already gone (collected elsewhere), so the
    entry is dropped either way.

    :param store: The store that just served a successful call.
    """
    from omnigent.db.db_models import workspace_scope

    with _lock:
        retry = list(_failed_deletes)
    for entry in retry:
        workspace_id, conv_id, elicit_id = entry
        with _persist_lock:
            with _lock:
                # Superseded by a re-publish while this sweep was running:
                # the fresh row must not be erased by the stale delete.
                if entry not in _failed_deletes:
                    continue
            try:
                with workspace_scope(workspace_id):
                    store.delete(conv_id, elicit_id)
            except Exception:
                return
            with _lock:
                _failed_deletes.discard(entry)


def restore_for(conversation_id: str) -> list[dict[str, Any]]:
    """
    Reload one session's outstanding prompts from the store into the index.

    Called whenever a session's index is empty. It deliberately does **not**
    take the conversation's mirrored count as a precondition: that count is
    written best-effort on a background executor, so a crash between the row
    commit and the count write leaves a durable row that a count-gated read
    would never ask for — which is precisely the crash this whole mechanism
    exists to survive.

    The cost of dropping that gate is bounded by remembering which
    conversations have already been looked up, so a process pays at most one
    indexed query per conversation however many times the session is loaded.
    Once the index holds an entry there is nothing to restore anyway.

    Prompts older than :data:`_MAX_PARK_SECONDS` are dropped: nothing can
    still be parked on them, so surfacing one would offer the user a button
    that resolves nothing. Their rows are deleted here — the table's contract
    is to hold only the parked set, and no other collector exists.

    After a successful read the count hook fires with the index's real count,
    so a persisted badge count that outlived its prompt (the runner timed out
    while the server was down) reconciles to what is actually answerable
    instead of staying lit forever.

    :param conversation_id: Session to restore, e.g. ``"conv_abc123"``.
    :returns: The restored event payloads, oldest first. Empty when no store
        is wired, the store fails, or nothing survives the age check.
    """
    store = _store
    if store is None:
        return []
    with _lock:
        if conversation_id in _restored:
            return []
        _restored.add(conversation_id)
    try:
        rows = store.list_for_conversation(conversation_id)
    except Exception:  # a failed restore degrades to the pre-mirror behaviour
        # Let the next read try again: a store that was briefly unavailable
        # must not cost this conversation its only restore for the lifetime of
        # the process.
        with _lock:
            _restored.discard(conversation_id)
        _logger.warning(
            "Could not restore outstanding elicitations for %s",
            conversation_id,
            exc_info=True,
        )
        return []
    cutoff = int(time.time()) - _MAX_PARK_SECONDS
    for row in rows:
        if row.created_at >= cutoff:
            continue
        # Same persist-lock rule as ``_persist_remove``: this age-out delete
        # must not land on top of a concurrent re-publish's fresh row.
        try:
            with _persist_lock:
                store.delete(conversation_id, row.id)
        except Exception:
            with _lock:
                _failed_deletes.add((row.workspace_id, conversation_id, row.id))
    with _lock:
        live = [
            row
            for row in rows
            if row.created_at >= cutoff and (conversation_id, row.id) not in _resolved_tombstones
        ]
        ids = _pending.setdefault(conversation_id, {})
        for row in live:
            ids.setdefault(row.id, row.event)
        restored = [copy.deepcopy(event) for event in ids.values()]
        count = len(ids)
        if not ids:
            _pending.pop(conversation_id, None)
    # Reconcile the persisted badge count with what is actually answerable.
    _notify_count_hook(conversation_id, count)
    return restored


def claim_descendant_restore(conversation_id: str) -> bool:
    """
    Claim the one-per-process descendant restore for an ancestor snapshot.

    After a restart the in-memory index is empty everywhere, so the cheap
    "anything pending anywhere?" pre-check that gates the descendant walk in
    ``GET /v1/sessions/{id}`` reports nothing — including for a child whose
    durable row is genuinely parked. The first snapshot of each ancestor per
    process therefore gets one walk regardless of that gate, letting
    :func:`restore_for` on each child pull its rows back. Subsequent
    snapshots return ``False`` and fall back to the in-memory gate.

    :param conversation_id: Ancestor session being snapshotted.
    :returns: ``True`` exactly once per conversation per process, and only
        when a store is wired (without one there is nothing to restore).
    """
    if _store is None:
        return False
    with _lock:
        if conversation_id in _descendants_probed:
            return False
        _descendants_probed.add(conversation_id)
        return True


def release_descendant_restore(conversation_id: str) -> None:
    """
    Hand back a descendant-restore claim whose walk could not complete.

    A transient store outage during the walk must not permanently consume the
    ancestor's only gate-free walk — the child prompt it would have surfaced
    stays hidden for the life of the process otherwise. The next snapshot of
    this ancestor claims and walks again.

    :param conversation_id: Ancestor whose claim to release.
    """
    with _lock:
        _descendants_probed.discard(conversation_id)


def needs_restore(conversation_id: str) -> bool:
    """
    Whether a session's durable rows have not yet been (re)loaded.

    ``True`` right after a failed :func:`restore_for` (the failure hands back
    the one-per-process claim), letting a caller that drove a batch of
    restores tell "restored, nothing there" apart from "could not ask".

    :param conversation_id: Session to check.
    :returns: ``True`` when a store is wired and the session has no completed
        restore recorded for this process.
    """
    if _store is None:
        return False
    with _lock:
        return conversation_id not in _restored


def record_publish(conversation_id: str, event: dict[str, Any]) -> None:
    """
    Update the index when an SSE event is published.

    Acts on two event types and silently ignores every other type —
    the function sits on the hot publish path so unrelated events
    must pay only one dict-key lookup.

    * ``response.elicitation_request`` — add the elicitation id +
      full event payload to the index. The payload is what
      :func:`snapshot_for` replays into ``GET /v1/sessions/{id}``
      so the UI can render the ApprovalCard on cold load.
    * ``response.elicitation_resolved`` — drop the elicitation id
      from the index. Used by the runner to clear an entry when
      its own ``_pending_approvals`` Future timed out or was
      cancelled without a UI verdict; same effect as
      :func:`resolve` but routed through the SSE chokepoint so the
      Omnigent server picks it up via ``_relay_runner_stream`` without
      a separate out-of-band signal.

    Idempotent on both event types — adds use a dict assignment
    (re-publishing the same id overwrites the payload) and drops are
    no-ops for unknown ids.

    After updating the index, a registered observer (if any — see
    :func:`set_elicitation_observer`) is notified with the same
    ``(conversation_id, event)`` so cross-session consumers (the
    parent-wake notifier) can react without re-deriving the event type.

    :param conversation_id: Conversation/session id the event was
        published on, e.g. ``"conv_abc123"``.
    :param event: The event dict as passed to
        :func:`omnigent.runtime.session_stream.publish`. Reads
        ``event["type"]`` to dispatch and ``event["elicitation_id"]``
        for both event types.
    """
    event_type = event.get("type")
    if event_type == "response.elicitation_request":
        elicitation_id = event.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        if _owns_elicitation(conversation_id, event):
            _persist_add(conversation_id, elicitation_id, event)
        with _lock:
            # A fresh publish reopens the id: deterministic harness ids repeat
            # across turns, and the new question must be restorable again.
            _resolved_tombstones.discard((conversation_id, elicitation_id))
            ids = _pending.setdefault(conversation_id, {})
            ids[elicitation_id] = event
            count = len(ids)
        _notify_count_hook(conversation_id, count)
        _notify_observer(conversation_id, event)
        return
    if event_type == "response.elicitation_resolved":
        elicitation_id = event.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        resolve(conversation_id, elicitation_id)
        _notify_observer(conversation_id, event)


def _notify_observer(conversation_id: str, event: dict[str, Any]) -> None:
    """
    Invoke the registered elicitation observer, if any.

    Read the module-global once into a local so a concurrent
    :func:`set_elicitation_observer` clearing it cannot turn the call
    into a ``None`` dereference between the check and the call.

    :param conversation_id: Conversation/session id the event was
        published on, e.g. ``"conv_abc123"``.
    :param event: The elicitation request/resolved event dict.
    :returns: None.
    """
    observer = _observer
    if observer is not None:
        observer(conversation_id, event)


def resolve(conversation_id: str, elicitation_id: str) -> None:
    """
    Drop an outstanding elicitation from the index.

    Called by the approval-dispatch path once a verdict has been
    accepted (regardless of which downstream awaiter — runner
    workflow or server-side ``_harness_elicitation_registry``
    Future — receives the verdict). Also called by
    :func:`record_publish` when a
    ``response.elicitation_resolved`` event passes through. The
    decrement here is the sidebar's signal that the session no
    longer needs attention.

    Idempotent: removing an id that isn't tracked is a no-op (the
    approval dispatch path may resolve an id whose publish landed
    on a different replica, or whose tracking failed validation).
    Empty conversation entries are popped so :func:`count_for`
    returns ``0`` cleanly without leaving stale keys.

    :param conversation_id: Conversation/session id the verdict
        was dispatched against, e.g. ``"conv_abc123"``.
    :param elicitation_id: The elicitation correlation id from the
        approval payload, e.g. ``"elicit_abc123"``.
    """
    # Tombstone first: an in-flight restore that already read this row must
    # not re-insert an answered prompt after we drop it below.
    with _lock:
        _resolved_tombstones.add((conversation_id, elicitation_id))
    # Unconditional, and before the index drop: this call may be the second
    # resolve for an id the index already forgot (the runner publishes a
    # resolved event on every exit path), and the row must go either way.
    _persist_remove(conversation_id, elicitation_id)
    with _lock:
        ids = _pending.get(conversation_id)
        if ids is None:
            return
        removed = ids.pop(elicitation_id, None) is not None
        count = len(ids)
        if not ids:
            _pending.pop(conversation_id, None)
    if removed:
        _notify_count_hook(conversation_id, count)


def count_for(conversation_id: str) -> int:
    """
    Return the number of outstanding elicitations for one session.

    :param conversation_id: Conversation/session id to query,
        e.g. ``"conv_abc123"``.
    :returns: Count of outstanding elicitations; ``0`` when the
        session has none tracked.
    """
    with _lock:
        ids = _pending.get(conversation_id)
        return len(ids) if ids is not None else 0


def counts_for(conversation_ids: list[str]) -> dict[str, int]:
    """
    Batch lookup of pending counts for a list of session ids.

    Used by ``GET /v1/sessions`` to populate the
    ``pending_elicitations_count`` field on each
    :class:`omnigent.server.schemas.SessionListItem` in one
    pass without re-acquiring the lock per session.

    :param conversation_ids: Conversation/session ids to query,
        e.g. ``["conv_abc123", "conv_def456"]``.
    :returns: Mapping from each id in the input to its outstanding
        count. Ids not tracked in the index map to ``0``.
    """
    with _lock:
        return {
            conv_id: len(_pending[conv_id]) if conv_id in _pending else 0
            for conv_id in conversation_ids
        }


def pending_session_ids() -> list[str]:
    """
    Return ids of every session with at least one outstanding elicitation.

    Used by ``GET /v1/sessions/{id}`` as a cheap pre-check before the
    descendant walk that mirrors child pending prompts into an ancestor
    snapshot — that walk costs one ``list_conversations`` query per
    session in the tree, so it should run only when some session other
    than the one being snapshotted actually has an outstanding prompt
    (the rare, transient case).

    :returns: Session ids with outstanding elicitations, e.g.
        ``["conv_child123"]``. Empty when nothing is pending anywhere.
    """
    with _lock:
        return list(_pending.keys())


def snapshot_for(conversation_id: str) -> list[dict[str, Any]]:
    """
    Return outstanding elicitation event payloads for one session.

    Used by ``GET /v1/sessions/{id}`` to replay outstanding
    ``response.elicitation_request`` events into the UI on cold
    load. Without replay, an elicitation emitted before the user
    opened the chat would never render — the SSE stream has no
    replay buffer.

    Returns deep copies of the stored event dicts so callers
    can mutate at any depth without poisoning the index. The
    elicitation event carries a nested ``params`` block; a
    shallow copy here would leak nested-dict mutations back into
    the index for subsequent reads.

    :param conversation_id: Conversation/session id to query,
        e.g. ``"conv_abc123"``.
    :returns: List of event dicts (each shaped like the original
        ``response.elicitation_request`` payload). Order is
        insertion order. Empty list when the session has no
        outstanding prompts.
    """
    with _lock:
        ids = _pending.get(conversation_id)
        if ids is None:
            return []
        return [copy.deepcopy(event) for event in ids.values()]


def project_for_peek(event: dict[str, Any]) -> dict[str, Any]:
    """
    Project a stored elicitation event into a compact peek item.

    ``sys_session_get_history`` returns a tail of compact conversation
    items so a parent agent can read a sub-agent's recent activity. A
    parked elicitation never lands in the conversation store (it lives
    only in this index — see the module docstring), so get_history must
    synthesize an item from the stored ``response.elicitation_request``
    payload. This projector produces that item, shaped to match the
    other compact items: a ``type`` discriminator plus the human-facing
    prompt and (form mode only) the fields being requested.

    Used by both read paths — the in-process
    :class:`omnigent.tools.builtins.spawn.SysSessionGetHistoryTool`
    (reads :func:`snapshot_for` directly) and the runner's REST
    get_history (reads the same payloads off the
    ``GET /v1/sessions/{id}`` snapshot). Both receive the identical
    event dict, so a single projector keeps the two outputs consistent.

    :param event: A stored ``response.elicitation_request`` event dict,
        as returned by :func:`snapshot_for`. Reads ``elicitation_id``
        and the nested ``params`` block (``message`` and, for form
        mode, ``requestedSchema.properties``).
    :returns: A compact dict, e.g.
        ``{"type": "pending_elicitation", "elicitation_id":
        "elicit_abc123", "prompt": "Approve running 'rm -rf'?",
        "fields": ["approve"]}``. ``prompt`` is ``None`` when the
        payload carried no message; ``fields`` is omitted when the
        elicitation is not a form (or has no declared properties).
    """
    params = event.get("params")
    params = params if isinstance(params, dict) else {}
    item: dict[str, Any] = {
        "type": "pending_elicitation",
        "elicitation_id": event.get("elicitation_id"),
        "prompt": params.get("message"),
    }
    schema = params.get("requestedSchema")
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            item["fields"] = list(properties.keys())
    return item


def lookup(elicitation_id: str) -> tuple[str, dict[str, Any]] | None:
    """
    Look up a single outstanding elicitation by its correlation id.

    Returns the ``(conversation_id, event_payload)`` pair if the
    elicitation is still pending, or ``None`` if it has already been
    resolved, timed out, or was never tracked. Used by the standalone
    approval page route to render the elicitation prompt without
    requiring a database round-trip — the in-memory index already
    holds the full event payload.

    :param elicitation_id: The elicitation correlation id to look up,
        e.g. ``"elicit_abc123"``.
    :returns: ``(conversation_id, event_dict)`` when found, ``None``
        otherwise. The event dict is a deep copy so callers cannot
        mutate the index.
    """
    with _lock:
        for conv_id, ids in _pending.items():
            if elicitation_id in ids:
                return conv_id, copy.deepcopy(ids[elicitation_id])
    return None


def reset_for_tests() -> None:
    """
    Clear the entire index. For test isolation only.

    Tests that exercise the publish or dispatch paths can mutate
    the module-global state; this resets between tests so leak
    from one test doesn't change the behavior of another. Not for
    production callers — there is no legitimate use case for
    wiping the index at runtime.
    """
    global _observer, _store
    with _lock:
        _pending.clear()
        _restored.clear()
        _descendants_probed.clear()
        _resolved_tombstones.clear()
        _failed_deletes.clear()
    _observer = None
    _store = None
