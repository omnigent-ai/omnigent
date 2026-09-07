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

Limitations:

* In-memory only; multi-replica Omnigent deploys would each see their own
  slice. This matches the existing ``_harness_elicitation_registry``
  constraint — when a shared backplane is added for the registry,
  this index should be wired through the same backplane.
* Events emitted before the Omnigent server starts (e.g. between turns,
  with the session_stream having dropped them) are not tracked,
  same as every other AP-server-side in-memory state.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any

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
        with _lock:
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
    global _observer
    with _lock:
        _pending.clear()
    _observer = None
