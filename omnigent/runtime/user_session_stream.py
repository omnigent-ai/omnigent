"""Per-user fan-out for session-list discovery events.

The ``WS /v1/sessions/updates`` push stream is *client-driven*: a browser
watches only the session ids it already has cached, so it can keep those rows
fresh but can never learn about a session created somewhere else (another tab,
the CLI, or one shared with the user) — that id was never in its watch-set.

This module closes that gap with a push instead of a poll. It is a tiny
fan-out broadcaster keyed by a *user key* (the authenticated user id, or a
shared sentinel in single-user mode): when a session becomes accessible to a
user, the HTTP route :func:`publish`es a ``session_added`` event, and every one
of that user's connected updates streams (each an async :func:`subscribe`)
wakes and pushes the new session to its browser. The browser then reconciles it
into the sidebar — so a new session appears within a tick of being created, and
an idle list still makes zero HTTP polls.

Mirrors :mod:`omnigent.runtime.session_stream` (the per-conversation SSE
broadcaster) but is deliberately minimal: no replay buffer, no end-of-stream
sentinel, no snapshot hooks, and no side-channels. Events emitted while a user
has no stream connected are simply dropped — that user's next page load fetches
the list over HTTP anyway, so there is nothing to recover. Kept free of
``omnigent.runtime`` imports so it can't introduce an import cycle.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache

# Subscriber registry: user_key -> set of (queue, event_loop) pairs, namespaced
# by workspace so the same user_id in two workspaces on one pod never shares a
# stream. The loop reference lets a publisher running on a different thread/loop
# deliver into the queue's owning loop via ``call_soon_threadsafe`` (matches
# session_stream).
_subscribers: WorkspaceScopedCache[
    str,
    set[tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]],
] = WorkspaceScopedCache()
_lock = threading.Lock()


def publish(user_key: str, event: dict[str, Any]) -> None:
    """
    Broadcast an event to every active subscriber for ``user_key``.

    No-op when that user has no stream connected (the common case), so callers
    can fire this unconditionally after a grant without checking for listeners.

    :param user_key: The target user's discovery key — the authenticated user
        id (e.g. ``"alice@example.com"``) in multi-user mode, or the shared
        single-user sentinel the updates route also subscribes under.
    :param event: The event dict to deliver, e.g.
        ``{"type": "session_added", "session_id": "conv_abc123"}``.
    """
    with _lock:
        subs = list(_subscribers.get(user_key, ()))
    for queue, loop in subs:
        loop.call_soon_threadsafe(queue.put_nowait, event)


async def subscribe(user_key: str) -> AsyncIterator[dict[str, Any]]:
    """
    Subscribe to discovery events for ``user_key`` until cancelled.

    Creates an ephemeral queue, registers it, and yields events as they arrive
    from :func:`publish`. Live-tail only — events emitted before this call are
    not replayed. The ``finally`` block always unregisters the slot, so a
    disconnected stream cannot leak a queue. Must be called from the event loop
    the caller iterates on.

    :param user_key: The user's discovery key to subscribe under (see
        :func:`publish`).
    :returns: An async iterator of event dicts, each yielded verbatim.
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    entry = (queue, loop)
    with _lock:
        _subscribers.setdefault(user_key, set()).add(entry)
    try:
        while True:
            yield await queue.get()
    finally:
        with _lock:
            subs = _subscribers.get(user_key)
            if subs is not None:
                subs.discard(entry)
                if not subs:
                    _subscribers.pop(user_key, None)
