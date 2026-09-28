"""Unit tests for :mod:`omnigent.runner.ingest_gate`.

The runner's message, compact, and next-turn handlers share a
per-conversation FIFO gate. These tests pin the contract that matters
for liveness: a reservation abandoned before its turn (a cancelled
queued request) must not wedge later requests for the conversation.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.runner.ingest_gate import IngestGate, IngestTicket


async def _settle() -> None:
    """Let every runnable task reach its next suspension point."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _queued(gate: IngestGate, conversation_id: str) -> asyncio.Task[IngestTicket]:
    task = asyncio.create_task(gate.acquire(conversation_id))
    await _settle()
    assert not task.done()
    return task


async def test_turns_run_in_arrival_order() -> None:
    gate = IngestGate()
    first = await gate.acquire("c")
    second = await _queued(gate, "c")
    third = await _queued(gate, "c")

    await gate.release(first)
    await _settle()
    assert second.done()
    assert not third.done()

    await gate.release(second.result())
    await _settle()
    assert third.done()
    await gate.release(third.result())


async def test_conversations_do_not_block_each_other() -> None:
    gate = IngestGate()
    held = await gate.acquire("a")
    other = await asyncio.wait_for(gate.acquire("b"), timeout=1)
    await gate.release(other)
    await gate.release(held)


async def test_cancelled_queued_request_does_not_wedge_conversation() -> None:
    """A message holds the slot, a compact queued behind it is cancelled
    during its wait, and a subsequent message must still run.
    """
    gate = IngestGate()
    message = await gate.acquire("c")
    compact = await _queued(gate, "c")
    later = await _queued(gate, "c")

    compact.cancel()
    with pytest.raises(asyncio.CancelledError):
        await compact

    await gate.release(message)
    ticket = await asyncio.wait_for(later, timeout=1)
    await gate.release(ticket)

    # The gate keeps working after skipping the abandoned number.
    await gate.release(await asyncio.wait_for(gate.acquire("c"), timeout=1))


async def test_consecutive_cancelled_reservations_are_all_skipped() -> None:
    gate = IngestGate()
    held = await gate.acquire("c")
    cancelled = [await _queued(gate, "c") for _ in range(3)]
    later = await _queued(gate, "c")

    # Cancel out of order to exercise the abandoned-set bookkeeping.
    for task in reversed(cancelled):
        task.cancel()
    await asyncio.gather(*cancelled, return_exceptions=True)

    await gate.release(held)
    await gate.release(await asyncio.wait_for(later, timeout=1))


async def test_cancel_after_turn_granted_still_advances() -> None:
    """A waiter cancelled in the same tick its turn arrives must not strand it."""
    gate = IngestGate()
    held = await gate.acquire("c")
    queued = await _queued(gate, "c")
    later = await _queued(gate, "c")

    await gate.release(held)
    queued.cancel()
    result = (await asyncio.gather(queued, return_exceptions=True))[0]
    if isinstance(result, IngestTicket):
        await gate.release(result)
    else:
        assert isinstance(result, asyncio.CancelledError)

    await gate.release(await asyncio.wait_for(later, timeout=1))


async def test_forget_isolates_recreated_conversation_from_stragglers() -> None:
    gate = IngestGate()
    old = await gate.acquire("c")
    gate.forget("c")

    fresh = await asyncio.wait_for(gate.acquire("c"), timeout=1)
    queued = await _queued(gate, "c")

    # A straggler from the torn-down conversation releasing late must not
    # advance the recreated conversation's queue.
    await gate.release(old)
    await _settle()
    assert not queued.done()

    await gate.release(fresh)
    await gate.release(await asyncio.wait_for(queued, timeout=1))
