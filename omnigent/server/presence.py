"""In-memory who-is-viewing registry for the session presence UI.

Tracks which users currently hold a live SSE stream open
(``GET /v1/sessions/{id}/stream``) anywhere in a session *tree* — the
root conversation or any sub-agent conversation under it — so the web
UI can render Google-Docs-style presence circles. Scoping presence to
the tree's root conversation is what lets two users on the same
session but different agents/sub-agents see each other: each
sub-agent page opens the *child* conversation's stream, and
per-conversation scoping would put them in disjoint viewer lists.

A "viewer" is a user with at least one open stream in the tree; each
open stream is one *connection*, keyed by a server-minted token, so
multiple tabs from the same user (even on different agents of the
same session) dedupe into a single viewer whose ``idle`` flag is the
AND of their tabs' flags.

Lifecycle (see ``designs/UI/PRESENCE.md``):

* ``connect`` on stream-generator entry — broadcasts the full viewer
  list on the user's 0→1 connection edge or when their idle
  aggregate changes.
* ``disconnect`` in the generator's ``finally`` — on the user's last
  connection closing, schedules the leave broadcast after
  :data:`_LEAVE_GRACE_S` instead of firing immediately. The grace
  window absorbs the Databricks Apps ingress' ~5-minute stream cap
  (every viewer transparently reconnects on that cadence), page
  refreshes, and root↔sub-agent navigation within one tree, so
  co-viewers' avatars don't flicker.
* ``snapshot`` — the current full viewer list as a
  ``session.presence`` event dict, emitted to each newly-connected
  stream via the snapshot-on-connect hook.

Every broadcast carries the FULL viewer list (never deltas) and is
published to every conversation stream in the tree that currently has
a registered viewer connection, with each event's ``conversation_id``
stamped as that stream's own conversation — clients guard incoming
events by the conversation they are viewing, so a tree-wide list must
arrive addressed to the stream it rides on. Missed or reordered
events self-heal on the next event or reconnect snapshot. Like
:mod:`omnigent.runtime.session_stream`, the registry is process-local
ephemeral state — it dies with the process, and all streams (whose
lifecycles define its contents) die with it too.

All mutating entry points run on the server's event loop (the SSE
route generator); the lock guards snapshot reads that may interleave
with the loop-callback leave timer under free-threaded access.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from omnigent.runtime import session_stream

# Delay between a user's last stream disconnecting and their leave
# being broadcast. Must comfortably exceed the client's transparent
# reconnect time so the ingress' ~5-minute stream-duration cap (see
# the ``stream_session`` route's header comment) doesn't make every
# viewer's avatar flicker for all co-viewers every 5 minutes.
_LEAVE_GRACE_S = 15.0


@dataclass
class _Connection:
    """
    One open viewer stream within a session tree.

    :param conversation_id: The conversation whose stream this
        connection holds open — the tree's root itself or any
        sub-agent conversation under it, e.g. ``"conv_child1"``.
        Broadcasts publish the tree-wide viewer list to each such
        stream.
    :param idle: The connection's idle flag, as reported by the
        client at connect time (tab backgrounded).
    """

    conversation_id: str
    idle: bool


@dataclass
class _ViewerEntry:
    """
    Presence state for one user within one session tree.

    :param joined_at: ISO 8601 UTC timestamp of the user's 0→1
        connection edge, e.g. ``"2026-06-10T17:00:00Z"``. Preserved
        across reconnects within the leave-grace window.
    :param connections: Open streams keyed by the server-minted
        connection token returned from :func:`connect`, e.g.
        ``{"a3f9…": _Connection("conv_child1", False)}``. Empty
        while the user is inside the leave-grace window.
    :param idle: The user-level idle aggregate (every connection
        idle). Recomputed on each connection change; frozen at its
        last value while ``connections`` is empty (grace window).
    """

    joined_at: str
    connections: dict[str, _Connection] = field(default_factory=dict)
    idle: bool = False


# root_conversation_id -> user_id -> entry. Keyed by the session
# tree's ROOT so viewers of different agents/sub-agents in one
# session share a single viewer list. Module-global, mirroring the
# subscriber registry in ``omnigent.runtime.session_stream``.
_viewers: dict[str, dict[str, _ViewerEntry]] = {}

# (root_conversation_id, user_id) -> pending leave-broadcast timer.
_pending_leaves: dict[tuple[str, str], asyncio.TimerHandle] = {}

_lock = threading.Lock()


def _now_iso() -> str:
    """
    Current UTC time as an ISO 8601 string with a ``Z`` suffix.

    :returns: Timestamp like ``"2026-06-10T17:00:00Z"``, matching
        the convention of :class:`SessionHeartbeatEvent.server_time`.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(root_id: str, conversation_id: str) -> dict[str, Any]:
    """
    Build the current full-state ``session.presence`` event.

    Used both as the per-stream broadcast payload on every presence
    change and as the snapshot-on-connect event a newly-subscribed
    stream receives, so clients have a single code path: replace
    their viewer list wholesale on every ``session.presence``.

    :param root_id: Root conversation of the session tree whose
        viewers to report, e.g. ``"conv_root123"``.
    :param conversation_id: The conversation whose stream this event
        is addressed to — the root itself or a sub-agent
        conversation, e.g. ``"conv_child1"``. Clients drop events
        whose ``conversation_id`` doesn't match the conversation
        they are viewing, so the tree-wide list must be stamped with
        the receiving stream's own id.
    :returns: Event dict shaped like
        ``{"type": "session.presence", "conversation_id": …,
        "viewers": [{"user_id": …, "joined_at": …, "idle": …}]}``,
        with viewers ordered by join time for stable rendering.
    """
    with _lock:
        entries = list(_viewers.get(root_id, {}).items())
    viewers = [
        {"user_id": user_id, "joined_at": entry.joined_at, "idle": entry.idle}
        for user_id, entry in sorted(entries, key=lambda item: item[1].joined_at)
    ]
    return {
        "type": "session.presence",
        "conversation_id": conversation_id,
        "viewers": viewers,
    }


def _broadcast(root_id: str) -> None:
    """
    Publish the tree's viewer list to every viewed stream in the tree.

    One event per distinct conversation stream — the root stream
    always (the session's canonical stream, preserving the
    pre-sub-agent-scoping behavior where the final leave broadcast
    still lands on it), plus every sub-agent stream that currently
    has a registered viewer connection — each stamped with that
    stream's own ``conversation_id`` (clients guard on it).
    Sub-agent streams without a registered viewer need no publish:
    in multi-user deployments every open session stream registers a
    connection, and a viewer inside the leave-grace window has no
    open stream to publish to. No-ops (inside
    ``session_stream.publish``) when nobody is subscribed;
    reconnecting clients repair state from the snapshot-on-connect
    event instead.

    :param root_id: Root conversation of the session tree to publish
        for, e.g. ``"conv_root123"``.
    """
    with _lock:
        users = _viewers.get(root_id, {})
        stream_ids = {root_id} | {
            connection.conversation_id
            for entry in users.values()
            for connection in entry.connections.values()
        }
    for stream_id in sorted(stream_ids):
        session_stream.publish(stream_id, snapshot(root_id, stream_id))


def connect(root_id: str, conversation_id: str, user_id: str, idle: bool) -> str:
    """
    Register one newly-opened viewer stream.

    Called from the SSE route generator on entry, after the route's
    access check, on the server's event loop. Cancels any pending
    leave broadcast for the user (a reconnect within the grace
    window — e.g. the ingress' ~5-minute stream cap, or navigating
    between agents of the same session — is invisible to co-viewers)
    and broadcasts the full viewer list when the user newly appears
    or their idle aggregate changes.

    :param root_id: Root conversation of the session tree being
        viewed, e.g. ``"conv_root123"`` — the
        ``root_conversation_id`` of the streamed conversation, so
        viewers of different agents/sub-agents in one session share
        a single presence scope.
    :param conversation_id: The conversation whose stream was opened
        — the root itself or a sub-agent conversation under it,
        e.g. ``"conv_child1"``.
    :param user_id: The authenticated viewer identity,
        e.g. ``"alice@example.com"``. Callers must pre-filter the
        reserved single-user sentinel via ``attribution_user`` —
        presence only tracks distinct human actors.
    :param idle: The connection's idle flag, computed by the client
        at connect time from tab visibility (``False`` for non-web
        consumers that don't send the query param).
    :returns: Server-minted connection token to pass back to
        :func:`disconnect`, e.g. ``"a3f9c2…"``.
    """
    token = uuid.uuid4().hex
    with _lock:
        timer = _pending_leaves.pop((root_id, user_id), None)
        users = _viewers.setdefault(root_id, {})
        entry = users.get(user_id)
        if entry is None:
            users[user_id] = _ViewerEntry(
                joined_at=_now_iso(),
                connections={token: _Connection(conversation_id, idle)},
                idle=idle,
            )
            changed = True
        else:
            entry.connections[token] = _Connection(conversation_id, idle)
            aggregate = all(connection.idle for connection in entry.connections.values())
            changed = aggregate != entry.idle
            entry.idle = aggregate
    if timer is not None:
        timer.cancel()
    if changed:
        _broadcast(root_id)
    return token


def disconnect(root_id: str, user_id: str, token: str) -> None:
    """
    Deregister one closed viewer stream.

    Called from the SSE route generator's ``finally`` on the event
    loop — every exit path (clean close, client disconnect,
    cancellation) lands here, so leave detection needs no client
    cooperation. On the user's last connection closing, the leave
    broadcast is deferred by :data:`_LEAVE_GRACE_S` via
    ``loop.call_later`` and cancelled if the user reconnects first.
    Closing a non-final connection still rebroadcasts when it flips
    the idle aggregate (e.g. the only *active* tab closed, leaving
    idle ones).

    :param root_id: Root conversation of the session tree that was
        being viewed — the value passed to :func:`connect`.
    :param user_id: The viewer identity passed to :func:`connect`.
    :param token: The connection token returned by :func:`connect`.
    """
    schedule_leave = False
    changed = False
    with _lock:
        entry = _viewers.get(root_id, {}).get(user_id)
        if entry is None or token not in entry.connections:
            return
        del entry.connections[token]
        if entry.connections:
            aggregate = all(connection.idle for connection in entry.connections.values())
            changed = aggregate != entry.idle
            entry.idle = aggregate
        else:
            schedule_leave = True
    if changed:
        _broadcast(root_id)
    if schedule_leave:
        handle = asyncio.get_running_loop().call_later(
            _LEAVE_GRACE_S,
            _expire_leave,
            root_id,
            user_id,
        )
        with _lock:
            _pending_leaves[(root_id, user_id)] = handle


def _expire_leave(root_id: str, user_id: str) -> None:
    """
    Leave-grace timer callback: finalize a departure and broadcast.

    Runs on the event loop via ``loop.call_later``. Removes the user
    only if they are still connection-less (a reconnect that raced
    the timer wins via the entry's repopulated ``connections``) and
    broadcasts the post-departure viewer list to the tree's
    remaining viewed streams.

    :param root_id: Root conversation of the session tree the user
        was viewing, e.g. ``"conv_root123"``.
    :param user_id: The departing viewer identity.
    """
    with _lock:
        _pending_leaves.pop((root_id, user_id), None)
        users = _viewers.get(root_id)
        if users is None:
            return
        entry = users.get(user_id)
        if entry is None or entry.connections:
            return
        del users[user_id]
        if not users:
            _viewers.pop(root_id, None)
    _broadcast(root_id)


def reset_for_tests() -> None:
    """
    Clear all presence state and cancel pending leave timers.

    Test-isolation hook mirroring
    ``omnigent.server._elicitation_registry.reset_for_tests`` —
    the registries are module-global, so an entry leaked by one test
    is visible to every later test in the same process.
    """
    with _lock:
        timers = list(_pending_leaves.values())
        _pending_leaves.clear()
        _viewers.clear()
    for timer in timers:
        timer.cancel()
