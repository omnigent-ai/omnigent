"""Per-user tick sharing for the ``WS /v1/sessions/updates`` stream.

A user with several tabs or devices holds one updates socket per tab, and
those sockets mostly watch the same sessions. Their tickers wake on one
shared per-user schedule, and each (user, session) row is built at most once
per tick and handed to every socket of that user that watches it. Each socket
still diffs against its own baseline, so the frames it sends are unchanged.

Across ticks, each user's conversations are reused while a cheap fingerprint
shows they haven't changed (see :class:`ConversationCache`).

State is per process and display-only: a replica shares only between the
sockets it holds itself, so it needs no cross-replica coordination.
"""

from __future__ import annotations

import asyncio
import math
import random
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Conversation
from omnigent.stores import ConversationStore

# Rows are user-specific (permission level, read state) and workspace-scoped,
# so sharing never crosses either boundary. Futures can't cross event loops.
_UserKey = tuple[asyncio.AbstractEventLoop, int, str | None]


class ConversationCache:
    """
    One user's conversations, reused across ticks while their fingerprints hold.

    A drop-in for :meth:`ConversationStore.get_conversations` on the tick
    path. Each call fingerprints every id and reloads only conversations
    whose fingerprint moved, or that were loaded more than
    ``_SESSION_UPDATES_FULL_RELOAD_INTERVAL_S`` ago, so an input the
    fingerprint misses is stale for at most that long. Access checks are not
    cached; the caller still runs them for every id on every tick.

    :param store: The conversation store to read through.
    """

    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        # Builds run in worker threads and may overlap across ticks.
        self._lock = threading.Lock()
        # id -> (fingerprint, conversation, monotonic load time)
        self._entries: dict[str, tuple[object, Conversation, float]] = {}

    def get_conversations(self, conversation_ids: list[str]) -> dict[str, Conversation]:
        """
        Return the conversations for ``conversation_ids``, reloading only changed ones.

        :param conversation_ids: Conversation ids to fetch,
            e.g. ``["conv_abc123"]``.
        :returns: Mapping ``{conversation_id: Conversation}``; ids without a
            row are omitted, exactly as from the store.
        """
        from omnigent.server.routes import sessions as _sf

        max_age_s = _sf._SESSION_UPDATES_FULL_RELOAD_INTERVAL_S
        fingerprints = self._store.get_conversation_fingerprints(conversation_ids)
        now = time.monotonic()
        found: dict[str, Conversation] = {}
        stale: list[str] = []
        with self._lock:
            self._entries = {
                cid: entry for cid, entry in self._entries.items() if now - entry[2] < max_age_s
            }
            for cid, fingerprint in fingerprints.items():
                entry = self._entries.get(cid)
                if entry is not None and entry[0] == fingerprint:
                    found[cid] = entry[1]
                else:
                    stale.append(cid)
        if stale:
            # Cache each reload under the fingerprint of the rows it was built
            # from, not the one read above: the row may have changed between
            # the two reads and could later change back to match it.
            loaded = self._store.get_conversations_with_fingerprints(stale)
            with self._lock:
                for cid, (fingerprint, conv) in loaded.items():
                    self._entries[cid] = (fingerprint, conv, now)
                    found[cid] = conv
        return found


RowBuilder = Callable[[list[str], str | None, ConversationCache], Awaitable[list[dict[str, Any]]]]


@dataclass
class _UserTicks:
    """Shared tick state for one user's open sockets."""

    conversations: ConversationCache
    # Offset of this user's schedule within the interval, as a fraction, so
    # different users' ticks spread out instead of landing together.
    phase: float = field(default_factory=random.random)
    sockets: int = 0
    tick: int | None = None
    # Build in flight or finished for each session id in the current tick.
    builds: dict[str, asyncio.Future[list[dict[str, Any]]]] = field(default_factory=dict)
    # Not-yet-started build, and the ids queued for it.
    batch: asyncio.Future[list[dict[str, Any]]] | None = None
    queued: list[str] = field(default_factory=list)


def _retrieve_exception(build: asyncio.Future[list[dict[str, Any]]]) -> None:
    """Mark a build's failure as seen when every waiting socket has gone away."""
    if not build.cancelled():
        build.exception()


class SessionUpdateTicks:
    """
    Share each tick's watched-row builds between one user's update sockets.

    :param build_rows: Builds the access-checked list-item dumps for some
        session ids on behalf of a user, reading conversations through that
        user's :class:`ConversationCache`, e.g. the stream's
        ``_fetch_watched_items``. Ids the user cannot see are omitted.
    :param store: The conversation store each user's cache reads through.
    """

    def __init__(self, build_rows: RowBuilder, store: ConversationStore) -> None:
        self._build_rows = build_rows
        self._store = store
        self._users: dict[_UserKey, _UserTicks] = {}

    @staticmethod
    def _key(user_id: str | None) -> _UserKey:
        return (asyncio.get_running_loop(), current_workspace_id(), user_id)

    def attach(self, user_id: str | None) -> int:
        """
        Register one open socket for ``user_id``.

        :param user_id: The socket's authenticated user, or ``None`` when
            permissions are disabled.
        :returns: The user's open sockets on this process, including this one.
        """
        key = self._key(user_id)
        user = self._users.get(key)
        if user is None:
            user = self._users[key] = _UserTicks(ConversationCache(self._store))
        user.sockets += 1
        return user.sockets

    def detach(self, user_id: str | None) -> int:
        """
        Release a socket registered by :meth:`attach`, dropping the user's
        shared state with their last socket.

        :param user_id: The user passed to :meth:`attach`.
        :returns: The user's open sockets on this process that remain.
        """
        key = self._key(user_id)
        user = self._users[key]
        user.sockets -= 1
        if user.sockets == 0:
            del self._users[key]
        return user.sockets

    async def next_tick(self, user_id: str | None, interval_s: float) -> tuple[int, float]:
        """
        Sleep until the user's next tick and return its number and start time.

        Every socket of the user wakes at the same instants, so a tick's
        shared rows are as fresh for the last socket as for the first.

        :param user_id: The attached user.
        :param interval_s: Tick interval in seconds, e.g. ``4.0``.
        :returns: The tick number to pass to :meth:`rows`, and the
            :func:`time.monotonic` instant it began. Its shared rows are all
            read after that instant.
        """
        offset = self._users[self._key(user_id)].phase * interval_s
        now = time.monotonic()
        tick = math.floor((now - offset) / interval_s) + 1
        starts_at = offset + tick * interval_s
        await asyncio.sleep(starts_at - now)
        return tick, starts_at

    async def rows(
        self, user_id: str | None, watched: list[str], tick: int
    ) -> list[dict[str, Any]]:
        """
        Return the user's current rows for ``watched``, building each once per tick.

        :param user_id: The attached user.
        :param watched: The socket's deduplicated watch-set,
            e.g. ``["conv_abc", "conv_def"]``.
        :param tick: The number returned by :meth:`next_tick`.
        :returns: One list-item dump per accessible watched session, in
            watch-set order. A failed build fails every socket that shares it
            during this tick; the next tick builds again.
        """
        user = self._users[self._key(user_id)]
        # A socket delayed past its tick reuses the newer tick's rows.
        if user.tick is None or tick > user.tick:
            user.tick = tick
            user.builds = {}
        missing = [sid for sid in watched if sid not in user.builds]
        if missing:
            if user.batch is None:
                user.batch = asyncio.ensure_future(self._build_batch(user, user_id))
                user.batch.add_done_callback(_retrieve_exception)
            user.queued.extend(missing)
            for sid in missing:
                user.builds[sid] = user.batch
        builds = {id(b): b for b in (user.builds[sid] for sid in watched)}.values()
        # Shield so a socket closing mid-build doesn't cancel rows others await.
        results = await asyncio.gather(*(asyncio.shield(b) for b in builds))
        by_id = {row["id"]: row for rows in results for row in rows}
        return [by_id[sid] for sid in watched if sid in by_id]

    async def _build_batch(self, user: _UserTicks, user_id: str | None) -> list[dict[str, Any]]:
        """Build every id queued while the user's sockets woke for this tick."""
        # Sockets woken by the same tick run before this resumes, so one
        # batch covers all of their ids.
        await asyncio.sleep(0)
        ids, user.queued, user.batch = user.queued, [], None
        return await self._build_rows(ids, user_id, user.conversations)
