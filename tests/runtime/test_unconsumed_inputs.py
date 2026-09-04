"""
Unit tests for :mod:`omnigent.runtime.unconsumed_inputs`.

The unconsumed-inputs index tracks user messages that were persisted and
steered into an already-running turn's buffer — delivered, but not yet
consumed by the agent loop. It backs the intermediate "sent, awaiting
the agent" state: the route layer records an entry when the runner
acknowledges a message as buffered, the relay resolves it when the
runner reports the message drained into a turn, snapshots replay the
still-pending ids for cold loads, and a terminal session status clears
the session wholesale. Tests here pin the module's core invariants:

* :func:`record` + :func:`snapshot_for` replay ids in FIFO (delivery)
  order.
* :func:`resolve` returns the recorded item exactly once and is
  idempotent (``None`` for unknown/already-resolved ids).
* :func:`clear` empties one conversation without touching others.
* Stale entries are evicted after :data:`unconsumed_inputs._TTL_S` —
  the leak bound for a drain marker that never arrives.

The wire-up (record on a buffered forward, resolve on the relay's drain
marker, replay in the snapshot, clear on terminal status) is covered by
the server route tests; this file tests the module in isolation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.runtime import unconsumed_inputs


@pytest.fixture(autouse=True)
def _clean_unconsumed_inputs_index() -> Iterator[None]:
    """
    Reset the module-global index between tests.

    The index is process-global; a leaked entry would change the
    snapshot behavior of every later test.
    """
    unconsumed_inputs.reset_for_tests()
    yield
    unconsumed_inputs.reset_for_tests()


def test_record_then_snapshot_replays_ids_in_delivery_order() -> None:
    """Snapshot lists recorded ids oldest-first, scoped per conversation."""
    unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"})
    unconsumed_inputs.record("conv_a", "item_2", {"id": "item_2"})
    unconsumed_inputs.record("conv_b", "item_9", {"id": "item_9"})

    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_1", "item_2"]
    assert unconsumed_inputs.snapshot_for("conv_b") == ["item_9"]
    assert unconsumed_inputs.snapshot_for("conv_unknown") == []


def test_resolve_returns_recorded_item_exactly_once() -> None:
    """Resolve hands back the recorded item, then reads as unknown."""
    item = {"id": "item_1", "data": {"role": "user"}}
    unconsumed_inputs.record("conv_a", "item_1", item)

    assert unconsumed_inputs.resolve("conv_a", "item_1") is item
    # Idempotent: the second resolve (a duplicate drain marker) is a no-op.
    assert unconsumed_inputs.resolve("conv_a", "item_1") is None
    assert unconsumed_inputs.snapshot_for("conv_a") == []


def test_resolve_unknown_id_is_noop() -> None:
    """Resolving an id that was never recorded returns ``None``."""
    assert unconsumed_inputs.resolve("conv_a", "item_missing") is None


def test_clear_drops_one_conversation_only() -> None:
    """A terminal-status clear empties its session, not its neighbors."""
    unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"})
    unconsumed_inputs.record("conv_b", "item_2", {"id": "item_2"})

    unconsumed_inputs.clear("conv_a")

    assert unconsumed_inputs.snapshot_for("conv_a") == []
    assert unconsumed_inputs.snapshot_for("conv_b") == ["item_2"]


def test_stale_entries_evicted_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries older than the TTL vanish from snapshots and resolves."""
    now = 1000.0
    monkeypatch.setattr(unconsumed_inputs, "_now", lambda: now)
    unconsumed_inputs.record("conv_a", "item_old", {"id": "item_old"})

    now += unconsumed_inputs._TTL_S + 1.0
    unconsumed_inputs.record("conv_a", "item_new", {"id": "item_new"})

    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_new"]
