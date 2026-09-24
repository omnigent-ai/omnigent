from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.runtime import unconsumed_inputs


@pytest.fixture(autouse=True)
def _clean_unconsumed_inputs_index() -> Iterator[None]:
    unconsumed_inputs.reset_for_tests()
    yield
    unconsumed_inputs.reset_for_tests()


def test_record_then_snapshot_replays_ids_in_delivery_order() -> None:
    unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"})
    unconsumed_inputs.record("conv_a", "item_2", {"id": "item_2"})
    unconsumed_inputs.record("conv_b", "item_9", {"id": "item_9"})

    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_1", "item_2"]
    assert unconsumed_inputs.snapshot_for("conv_b") == ["item_9"]
    assert unconsumed_inputs.snapshot_for("conv_unknown") == []


def test_resolve_returns_recorded_item_exactly_once() -> None:
    item = {"id": "item_1", "data": {"role": "user"}}
    unconsumed_inputs.record("conv_a", "item_1", item)

    assert unconsumed_inputs.resolve("conv_a", "item_1") is item
    assert unconsumed_inputs.resolve("conv_a", "item_1") is None
    assert unconsumed_inputs.snapshot_for("conv_a") == []


def test_resolve_unknown_id_is_noop() -> None:
    assert unconsumed_inputs.resolve("conv_a", "item_missing") is None


def test_clear_drops_one_conversation_only() -> None:
    unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"})
    unconsumed_inputs.record("conv_b", "item_2", {"id": "item_2"})

    unconsumed_inputs.clear("conv_a")

    assert unconsumed_inputs.snapshot_for("conv_a") == []
    assert unconsumed_inputs.snapshot_for("conv_b") == ["item_2"]


def test_stale_entries_evicted_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(unconsumed_inputs, "_now", lambda: now)
    unconsumed_inputs.record("conv_a", "item_old", {"id": "item_old"})

    now += unconsumed_inputs._TTL_S + 1.0
    unconsumed_inputs.record("conv_a", "item_new", {"id": "item_new"})

    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_new"]


def test_record_after_resolve_reports_already_drained() -> None:
    assert unconsumed_inputs.resolve("conv_a", "item_1") is None

    assert unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"}) is False
    assert unconsumed_inputs.snapshot_for("conv_a") == []
    assert unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"}) is True
    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_1"]


def test_pre_drained_mark_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(unconsumed_inputs, "_now", lambda: now)
    assert unconsumed_inputs.resolve("conv_a", "item_1") is None

    now += unconsumed_inputs._PRE_DRAINED_TTL_S + 1.0
    assert unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"}) is True
    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_1"]


def test_clear_drops_pre_drained_marks() -> None:
    assert unconsumed_inputs.resolve("conv_a", "item_1") is None

    unconsumed_inputs.clear("conv_a")

    assert unconsumed_inputs.record("conv_a", "item_1", {"id": "item_1"}) is True
    assert unconsumed_inputs.snapshot_for("conv_a") == ["item_1"]
