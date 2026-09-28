"""Per-conversation FIFO gate for runner ingest handlers.

Message, compact, and next-turn handlers each reserve a sequence number for
their conversation, wait until that number is being served, do their
check-then-bind work, and then advance the counter so the next reservation
runs. That keeps two handlers from both seeing a conversation idle across an
``await`` and binding the same turn slot.

Lifted out of ``omnigent.runner.app`` so the reservation is released on every
exit path, including cancellation while a handler is still queued. A queued
reservation that is abandoned before its turn is remembered and skipped when
the counter reaches it; without that, the counter stops at the abandoned
number and every later request for the conversation waits forever.

Lifecycle contract:

* :meth:`IngestGate.acquire` reserves the next number and waits for it. If it
  raises (for example ``CancelledError``), the reservation is already released
  and the caller must not call :meth:`IngestGate.release`.
* After a successful ``acquire``, the caller MUST call
  :meth:`IngestGate.release` in a ``finally`` block.
* :meth:`IngestGate.forget` drops a conversation's state. Tickets issued
  before it keep working against the dropped state, so a same-id conversation
  created afterwards starts a fresh sequence unaffected by stragglers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class _Lane:
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    next_seq: int = 0
    now_serving: int = 0
    # Reservations released before their turn, skipped when reached.
    abandoned: set[int] = field(default_factory=set)

    def release(self, seq: int) -> None:
        """Mark ``seq`` done. Caller must hold ``cond``."""
        if seq != self.now_serving:
            self.abandoned.add(seq)
            return
        serving = seq + 1
        while serving in self.abandoned:
            self.abandoned.discard(serving)
            serving += 1
        self.now_serving = serving
        self.cond.notify_all()


@dataclass(frozen=True)
class IngestTicket:
    """A granted turn at a conversation's ingest gate."""

    lane: _Lane
    seq: int


class IngestGate:
    """FIFO admission for ingest handlers, one queue per conversation."""

    def __init__(self) -> None:
        self._lanes: dict[str, _Lane] = {}

    async def acquire(self, conversation_id: str) -> IngestTicket:
        """Reserve the next number for ``conversation_id`` and wait for it."""
        lane = self._lanes.get(conversation_id)
        if lane is None:
            lane = _Lane()
            self._lanes[conversation_id] = lane
        # Reserve under the lock: a cancel while waiting for the lock then
        # leaves nothing reserved. The lock is FIFO and never held across an
        # await other than ``wait``, so arrival order is preserved.
        async with lane.cond:
            seq = lane.next_seq
            lane.next_seq = seq + 1
            try:
                while lane.now_serving != seq:
                    await lane.cond.wait()
            except BaseException:
                # ``Condition.wait`` re-acquires the lock before raising, so
                # the release below still runs under the lock.
                lane.release(seq)
                raise
        return IngestTicket(lane=lane, seq=seq)

    async def release(self, ticket: IngestTicket) -> None:
        """Advance past ``ticket`` so the next reservation can run."""
        async with ticket.lane.cond:
            ticket.lane.release(ticket.seq)

    def forget(self, conversation_id: str) -> None:
        """Drop ``conversation_id``'s state when the conversation is torn down."""
        self._lanes.pop(conversation_id, None)
