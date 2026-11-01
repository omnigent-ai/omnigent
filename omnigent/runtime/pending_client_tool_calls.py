"""In-process index of outstanding client-side tool calls.

A client-declared tool dispatches to ``action_required`` and parks the
turn until the caller posts a ``function_call_output``. The live SSE
stream has no replay: a client that disconnects while the call is
outstanding and later reconnects would otherwise never learn the turn
is waiting on it. This index mirrors
:mod:`omnigent.runtime.pending_elicitations` so ``GET
/v1/sessions/{id}`` can replay outstanding client-tool calls into a
reconnecting client's snapshot, the same way it replays outstanding
elicitation prompts.

Populated by :func:`omnigent.runtime.session_stream.publish` whenever a
``response.output_item.done`` event carrying an ``action_required``
function_call passes through — excluding a call whose item marks
``client_owned: False`` (a server-executed dispatch, e.g. the host-tool
bridge or an MCP/spec-local tool): a reconnecting client has nothing to
answer for a call it never owned. Drained either by the matching
``function_call_output`` flowing back through the same publish
chokepoint (the scaffold harness's own round-trip), by a
``session.input.consumed`` event whose data is a ``function_call_output``
(the native-mirrored accept path), or directly by the ``POST
/v1/sessions/{id}/events`` handler once it has forwarded the output to
the bound runner — whichever observes the answer first.

The index stores the full event payload (not just the call id) so a
reconnecting client's snapshot is byte-identical to what the live
stream would have shown. Like ``pending_elicitations``, it lives and
dies with the in-process awaiter: a dead index entry can never claim a
call is outstanding when the harness has already moved on, because
both the Future and the index entry are cleared together on every
terminal or interrupt path.

Limitations (same as ``pending_elicitations``):

* In-memory only; multi-replica deployments each see their own slice.
* Lost on server restart, same as the live SSE stream itself. A
  parked function_call item is deliberately never persisted to the
  conversation store (see ``_extract_persistent_item_from_sse``), so
  there is no durable row to recover it from either — a restart loses
  the same visibility the web UI's tool-call bubble already loses
  today, not something new this index introduces.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache

# Per-conversation mapping of outstanding call_id → original
# ``response.output_item.done`` event payload. Populated by
# ``record_publish`` on an ``action_required`` function_call; drained
# by ``resolve`` (called directly once a ``function_call_output`` is
# accepted on ``POST /events``, or via ``record_publish`` when the
# matching output / ``session.input.consumed`` event flows through the
# SSE chokepoint) and by ``clear_for`` on a turn's terminal or
# interrupt event. Empty inner dicts are popped eagerly so
# :func:`snapshot_for` doesn't see stale keys.
_pending: WorkspaceScopedCache[str, dict[str, dict[str, Any]]] = WorkspaceScopedCache()
_lock = threading.Lock()

# A conversation has at most one in-flight turn (see
# ``runtime/harnesses/_scaffold.py``), so any of these terminal /
# interrupt events unambiguously ends every client-tool call still
# parked for that conversation — there is no other turn they could
# belong to.
_TURN_END_EVENT_TYPES = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
        "session.interrupted",
    }
)


def record_publish(conversation_id: str, event: dict[str, Any]) -> None:
    """
    Update the index when an SSE event is published.

    Sits on the hot publish path, so every event type this function
    does not recognize costs exactly one ``dict.get`` before
    returning.

    * ``response.output_item.done`` with a ``function_call`` item
      whose ``status`` is ``"action_required"`` — add the call to the
      index, keyed by ``call_id``.
    * ``response.output_item.done`` with a ``function_call_output``
      item, or ``session.input.consumed`` whose ``data.type`` is
      ``"function_call_output"`` — drain the matching call_id. Covers
      both the scaffold harness's own round-trip (the former) and the
      native-mirrored accept path (the latter).
    * Any terminal or interrupt event type in
      :data:`_TURN_END_EVENT_TYPES` — drain every call still
      outstanding for this conversation. A cancelled call never emits
      its own completion event, so this is the only signal that it is
      no longer answerable.

    Every branch is idempotent: re-publishing an ``action_required``
    event overwrites the stored payload for the same id; draining an
    unknown or already-drained id is a no-op.

    :param conversation_id: Conversation/session id the event was
        published on, e.g. ``"conv_abc123"``.
    :param event: The event dict as passed to
        :func:`omnigent.runtime.session_stream.publish`.
    """
    event_type = event.get("type")
    if event_type == "response.output_item.done":
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "function_call":
            if item.get("status") != "action_required":
                return
            # Only a genuinely client-owned call belongs in the reconnect
            # snapshot — a server-executed dispatch (host-tool bridge, MCP,
            # spec-local) is never answerable by a reconnecting client.
            # Missing the marker defaults to tracked (safe: a call from any
            # future caller that doesn't set it is visible rather than
            # silently hidden).
            if item.get("client_owned") is False:
                return
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return
            with _lock:
                _pending.setdefault(conversation_id, {})[call_id] = event
            return
        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                resolve(conversation_id, call_id)
            return
        return
    if event_type == "session.input.consumed":
        data = event.get("data")
        if isinstance(data, dict) and data.get("type") == "function_call_output":
            output_data = data.get("data")
            call_id = output_data.get("call_id") if isinstance(output_data, dict) else None
            if isinstance(call_id, str) and call_id:
                resolve(conversation_id, call_id)
        return
    if event_type in _TURN_END_EVENT_TYPES:
        clear_for(conversation_id)


def resolve(conversation_id: str, call_id: str) -> None:
    """
    Drop an outstanding client-tool call from the index.

    Called directly once ``POST /v1/sessions/{id}/events`` has
    forwarded a ``function_call_output`` to the bound runner (the
    first accepted answer drains the entry immediately, without
    waiting for the round-trip event to flow back through the stream),
    and by :func:`record_publish` when the matching output / consumed
    event is observed on the stream.

    Idempotent: removing an id that isn't tracked — because it was
    already drained, expired, or never seen on this replica — is a
    no-op, which is what makes a duplicate or late answer harmless.

    :param conversation_id: Conversation/session id the answer was
        posted against, e.g. ``"conv_abc123"``.
    :param call_id: The tool call id from the posted
        ``function_call_output``, e.g. ``"call_abc123"``.
    """
    with _lock:
        calls = _pending.get(conversation_id)
        if calls is None:
            return
        calls.pop(call_id, None)
        if not calls:
            _pending.pop(conversation_id, None)


def clear_for(conversation_id: str) -> None:
    """
    Drop every outstanding client-tool call for one conversation.

    Called when the in-flight turn ends — completed, failed,
    cancelled, incomplete, or interrupted — so a call a harness
    cancelled without emitting its own terminal event (``_cancel_pending``
    unblocks ``dispatch_tool`` with no completion event) can never be
    shown as still answerable.

    :param conversation_id: Conversation/session id whose turn ended,
        e.g. ``"conv_abc123"``.
    """
    with _lock:
        _pending.pop(conversation_id, None)


def snapshot_for(conversation_id: str) -> list[dict[str, Any]]:
    """
    Return outstanding client-tool call event payloads for one session.

    Used by ``GET /v1/sessions/{id}`` to replay outstanding
    ``response.output_item.done`` (action_required) events into a
    reconnecting client's snapshot — the SSE stream has no replay, so a
    call parked before the client (re)connected would otherwise never
    render as answerable.

    Returns deep copies so callers can mutate at any depth without
    poisoning the index.

    :param conversation_id: Conversation/session id to query,
        e.g. ``"conv_abc123"``.
    :returns: List of event dicts, each shaped like the original
        ``response.output_item.done`` payload, in insertion order.
        Empty when the session has no outstanding client-tool calls.
    """
    with _lock:
        calls = _pending.get(conversation_id)
        if calls is None:
            return []
        return [copy.deepcopy(event) for event in calls.values()]


def reset_for_tests() -> None:
    """
    Clear the entire index. For test isolation only.

    Not for production callers — there is no legitimate use case for
    wiping the index at runtime.
    """
    with _lock:
        _pending.clear()
