"""Process-local registry of the follow-ups each client holds queued for a session.

The queue itself stays in the client that typed the message (the body and
attachments never reach the server until the message is POSTed); this module
keeps the session-wide *view* of those queues. Each client publishes its share
(``PUT /v1/sessions/{id}/queue``); the registry merges shares into one FIFO list
ordered by a per-conversation sequence, broadcasts it as ``session.queue``, and
drops a share :data:`_DETACH_GRACE_S` after the client's last stream for the
conversation closes (:func:`attach` / :func:`detach`, driven by the stream
route). See ``docs/QUEUE_STEER_DESIGN.md`` for the design.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.runtime import session_stream
from omnigent.server.schemas import QueuedMessageInput

# Delay between a client's last stream for a conversation closing and its
# share being dropped. Mirrors presence's leave grace so the ingress'
# ~5-minute stream cap and page refreshes don't flicker the queue.
_DETACH_GRACE_S = 15.0

# Max populated shares per user per conversation (each change rebroadcasts the
# whole merged list). Stream-less excess is evicted oldest first; a populated
# share whose client holds a stream is refused instead of evicting another's.
MAX_SHARES_PER_USER = 8

_ShareKey = tuple[str, str]


class ShareLimitExceeded(Exception):
    """The user already holds the maximum populated shares for the conversation."""


@dataclass
class _Entry:
    """
    One queued follow-up as published by its owning client.

    :param queue_id: The owner's client-local id, e.g. ``"q_3"``.
    :param seq: Per-conversation ordering slot; lower flushes first.
    :param text: Message text (display only; the owner holds the real payload).
    :param attachments: Attachment filenames for the strip's badge.
    :param stable_id: The owner's idempotency id for the eventual POST.
    :param requires_retry: The owner's send failed; it waits for the user.
    """

    queue_id: str
    seq: int
    text: str
    attachments: list[str]
    stable_id: str | None
    requires_retry: bool


@dataclass
class _Share:
    """
    One client's queued entries for one conversation plus its stream count.

    :param created_by: Attribution identity of the publishing user, or
        ``None`` in single-user mode.
    :param entries: The client's entries in its own order.
    :param connections: Open streams this client holds for the conversation.
    """

    created_by: str | None
    entries: list[_Entry] = field(default_factory=list)
    connections: int = 0


@dataclass
class _ConversationQueue:
    """
    Every client's share for one conversation.

    :param shares: ``(user_key, client_id)`` → that client's share.
    :param next_seq: Next ordering slot to hand out.
    """

    shares: dict[_ShareKey, _Share] = field(default_factory=dict)
    next_seq: int = 1


_queues: WorkspaceScopedCache[str, _ConversationQueue] = WorkspaceScopedCache()
_pending_expiries: WorkspaceScopedCache[tuple[str, _ShareKey], asyncio.TimerHandle] = (
    WorkspaceScopedCache()
)
_lock = threading.Lock()


def _share_key(user_id: str | None, client_id: str) -> _ShareKey:
    return (user_id or "", client_id)


def snapshot(conversation_id: str) -> dict[str, Any]:
    """
    Build the current full-state ``session.queue`` event.

    Used both as the broadcast payload on every change and as the
    snapshot-on-connect event a newly-subscribed stream receives, so
    clients replace their view wholesale on every ``session.queue``.

    :param conversation_id: The conversation whose queue to report.
    :returns: Event dict shaped like ``{"type": "session.queue",
        "conversation_id": …, "messages": [{"queue_id", "client_id", "seq",
        "text", "attachments", "stable_id", "created_by", "requires_retry"}]}``
        with messages in flush order.
    """
    with _lock:
        queue = _queues.get(conversation_id)
        rows = (
            [
                (entry.seq, client_id, share.created_by, entry)
                for (_user, client_id), share in queue.shares.items()
                for entry in share.entries
            ]
            if queue is not None
            else []
        )
    rows.sort(key=lambda row: row[0])
    return {
        "type": "session.queue",
        "conversation_id": conversation_id,
        "messages": [
            {
                "queue_id": entry.queue_id,
                "client_id": client_id,
                "seq": seq,
                "text": entry.text,
                "attachments": list(entry.attachments),
                "stable_id": entry.stable_id,
                "created_by": created_by,
                "requires_retry": entry.requires_retry,
            }
            for seq, client_id, created_by, entry in rows
        ],
    }


def _broadcast(conversation_id: str) -> None:
    session_stream.publish(conversation_id, snapshot(conversation_id))


def _assign_entries(
    queue: _ConversationQueue, previous: list[_Entry], messages: list[QueuedMessageInput]
) -> list[_Entry]:
    """
    Build a share's new entry list, preserving ordering slots where possible.

    Entries the client still holds take the share's existing slots in the
    client's new order, so a pure reorder keeps the share's position among
    other clients. Entries appended at the tail get fresh slots. Once a new
    entry appears ahead of surviving ones, every later entry also gets a fresh
    slot: the client's own order always wins over slot reuse.
    """
    surviving = {entry.queue_id for entry in previous} & {m.queue_id for m in messages}
    slots = iter(sorted(entry.seq for entry in previous if entry.queue_id in surviving))
    reuse_slots = True
    entries: list[_Entry] = []
    seen: set[str] = set()
    for message in messages:
        if message.queue_id in seen:
            continue
        seen.add(message.queue_id)
        if reuse_slots and message.queue_id in surviving:
            seq = next(slots)
        else:
            reuse_slots = False
            seq = queue.next_seq
            queue.next_seq += 1
        entries.append(
            _Entry(
                queue_id=message.queue_id,
                seq=seq,
                text=message.text,
                attachments=list(message.attachments),
                stable_id=message.stable_id,
                requires_retry=message.requires_retry,
            )
        )
    return entries


def replace(
    conversation_id: str,
    *,
    client_id: str,
    user_id: str | None,
    messages: list[QueuedMessageInput],
) -> None:
    """
    Replace one client's queued entries for a conversation and broadcast.

    :param conversation_id: Session/conversation identifier.
    :param client_id: The publishing SPA instance's id, e.g. ``"c_7f3a…"``.
    :param user_id: Attribution identity of the caller (``None`` single-user).
    :param messages: The client's complete current queue for this
        conversation, head first. An empty list clears its share.
    :raises ShareLimitExceeded: If the share would become populated while
        the user already holds :data:`MAX_SHARES_PER_USER` populated shares
        for the conversation whose clients all hold streams (stream-less ones
        beyond the cap are dropped instead, oldest first).
    """
    key = _share_key(user_id, client_id)
    schedule_expiry = False
    with _lock:
        queue = _queues.setdefault(conversation_id, _ConversationQueue())
        share = queue.shares.get(key)
        previous = share.entries if share is not None else []
        changed = False
        if messages and not previous:
            changed = _make_room_locked(conversation_id, queue, key)
        entries = _assign_entries(queue, previous, messages)
        changed = changed or entries != previous
        if share is None:
            share = _Share(created_by=user_id)
            queue.shares[key] = share
        share.entries = entries
        if share.connections == 0:
            if not entries:
                _drop_share_locked(conversation_id, queue, key)
            elif (conversation_id, key) not in _pending_expiries:
                schedule_expiry = True
    if schedule_expiry:
        _schedule_expiry(conversation_id, key)
    if changed:
        _broadcast(conversation_id)


def attach(conversation_id: str, *, client_id: str, user_id: str | None) -> None:
    """
    Register one newly-opened stream of a client for a conversation.

    Cancels a pending expiry so a reconnect within the grace window keeps
    the client's entries visible to everyone else.
    """
    key = _share_key(user_id, client_id)
    with _lock:
        queue = _queues.setdefault(conversation_id, _ConversationQueue())
        share = queue.shares.setdefault(key, _Share(created_by=user_id))
        share.connections += 1
        timer = _pending_expiries.pop((conversation_id, key), None)
    if timer is not None:
        timer.cancel()


def detach(conversation_id: str, *, client_id: str, user_id: str | None) -> None:
    """
    Deregister one closed stream; on the client's last one, start the grace timer.
    """
    key = _share_key(user_id, client_id)
    schedule_expiry = False
    with _lock:
        queue = _queues.get(conversation_id)
        share = queue.shares.get(key) if queue is not None else None
        if share is None or share.connections == 0:
            return
        share.connections -= 1
        if share.connections == 0:
            if share.entries:
                schedule_expiry = (conversation_id, key) not in _pending_expiries
            else:
                assert queue is not None
                _drop_share_locked(conversation_id, queue, key)
    if schedule_expiry:
        _schedule_expiry(conversation_id, key)


def _schedule_expiry(conversation_id: str, key: _ShareKey) -> None:
    handle = asyncio.get_running_loop().call_later(_DETACH_GRACE_S, _expire, conversation_id, key)
    with _lock:
        _pending_expiries[(conversation_id, key)] = handle


def _expire(conversation_id: str, key: _ShareKey) -> None:
    """Grace-timer callback: drop a still-disconnected client's share and broadcast."""
    with _lock:
        _pending_expiries.pop((conversation_id, key), None)
        queue = _queues.get(conversation_id)
        share = queue.shares.get(key) if queue is not None else None
        if queue is None or share is None or share.connections:
            return
        had_entries = bool(share.entries)
        _drop_share_locked(conversation_id, queue, key)
    if had_entries:
        _broadcast(conversation_id)


def _make_room_locked(conversation_id: str, queue: _ConversationQueue, keep: _ShareKey) -> bool:
    """
    Make room for *keep* to become populated among its user's shares.

    :returns: Whether any stream-less share was dropped.
    :raises ShareLimitExceeded: If the shares that would have to go hold streams.
    """
    others = [
        (other, share)
        for other, share in queue.shares.items()
        if other[0] == keep[0] and other != keep and share.entries
    ]
    excess = len(others) - (MAX_SHARES_PER_USER - 1)
    if excess <= 0:
        return False
    detached = [other for other, share in others if share.connections == 0]
    if len(detached) < excess:
        raise ShareLimitExceeded(
            f"at most {MAX_SHARES_PER_USER} populated queue shares per user and conversation"
        )
    for other in detached[:excess]:
        _drop_share_locked(conversation_id, queue, other)
    return True


def _drop_share_locked(conversation_id: str, queue: _ConversationQueue, key: _ShareKey) -> None:
    queue.shares.pop(key, None)
    if not queue.shares:
        _queues.pop(conversation_id, None)
    # A stale timer would otherwise cut the next share's grace short.
    timer = _pending_expiries.pop((conversation_id, key), None)
    if timer is not None:
        timer.cancel()


def reset_for_tests() -> None:
    """Clear all shares and cancel pending expiry timers (test isolation)."""
    with _lock:
        timers = _pending_expiries.all_values()
        _pending_expiries.clear()
        _queues.clear()
    for timer in timers:
        timer.cancel()
