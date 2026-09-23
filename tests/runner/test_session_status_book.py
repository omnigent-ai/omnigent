"""Tests for the runner's session status book."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Mapping

import pytest

from omnigent.runner.session_status import (
    LOCAL_SOURCES,
    SessionStatusBook,
    StatusSource,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _book() -> tuple[SessionStatusBook, _Clock]:
    clock = _Clock()
    return SessionStatusBook(clock=clock, wall_clock=lambda: 1.7e9), clock


def test_duplicate_keeps_since_and_accumulates_sources() -> None:
    book, clock = _book()
    assert book.record("conv", "running", source=StatusSource.RUNNER) is True
    clock.now += 30
    assert book.record("conv", "running", source=StatusSource.RELAY) is False
    record = book.current("conv")
    assert record is not None
    assert record.since == 1000.0
    assert record.last_seen == 1030.0
    assert record.sources == {StatusSource.RUNNER, StatusSource.RELAY}
    assert record.origin is StatusSource.RUNNER


def test_status_change_starts_a_new_episode() -> None:
    book, clock = _book()
    book.record("conv", "running", source=StatusSource.PTY)
    book.record("conv", "running", source=StatusSource.RELAY)
    clock.now += 5
    assert book.record("conv", "idle", source=StatusSource.RELAY) is True
    record = book.current("conv")
    assert record is not None
    assert record.status == "idle"
    assert record.since == 1005.0
    assert record.sources == {StatusSource.RELAY}


def test_claim_excludes_waiting_and_relay_only_running() -> None:
    book, _ = _book()
    book.record("conv", "waiting", source=StatusSource.RUNNER)
    assert book.claim("conv") is None
    assert book.claim("conv", include_relay=True) is None

    book.record("conv", "running", source=StatusSource.RELAY)
    assert book.claim("conv") is None
    relay_claim = book.claim("conv", include_relay=True)
    assert relay_claim is not None
    assert relay_claim.sources == {StatusSource.RELAY}

    book.record("conv", "running", source=StatusSource.PTY)
    local_claim = book.claim("conv")
    assert local_claim is not None
    assert local_claim.sources == {StatusSource.RELAY, StatusSource.PTY}


def test_blocked_on_change_keeps_since_and_restamps_blocked_since() -> None:
    book, clock = _book()
    book.record("conv", "running", source=StatusSource.STATUS_FILE)
    clock.now += 10
    assert (
        book.record(
            "conv", "running", source=StatusSource.STATUS_FILE, blocked_on="permission prompt"
        )
        is True
    )
    clock.now += 7
    record = book.current("conv")
    assert record is not None
    assert record.since == 1000.0
    assert book.blocked("conv") == ("permission prompt", 7.0)


def test_blocked_on_clears_only_from_the_channel_that_set_it() -> None:
    book, _ = _book()
    book.record("conv", "running", source=StatusSource.STATUS_FILE, blocked_on="dialog open")
    book.record("conv", "running", source=StatusSource.RELAY)
    assert book.blocked("conv") is not None
    book.record("conv", "running", source=StatusSource.STATUS_FILE)
    assert book.blocked("conv") is None


def test_blocked_is_none_once_the_session_is_idle() -> None:
    book, _ = _book()
    book.record("conv", "running", source=StatusSource.STATUS_FILE, blocked_on="dialog open")
    book.record("conv", "idle", source=StatusSource.STATUS_FILE)
    assert book.blocked("conv") is None


def test_reset_keeps_the_ordering_stamps() -> None:
    book, clock = _book()
    book.record("conv", "running", source=StatusSource.RUNNER)
    dispatched = book.last_dispatch_at("conv")
    clock.now += 5
    book.record("conv", "idle", source=StatusSource.CONTROL)
    control = book.last_control_idle_at("conv")

    book.reset("conv", "native_terminal_closed")

    assert book.current("conv") is None
    # They order evidence that outlives the pane (a hook log on disk).
    assert book.last_dispatch_at("conv") == dispatched
    assert book.last_control_idle_at("conv") == control


def test_forget_drops_everything() -> None:
    book, _ = _book()
    book.record("conv", "idle", source=StatusSource.CONTROL)
    book.forget("conv")
    assert book.current("conv") is None
    assert book.last_control_idle_at("conv") is None


def test_transfer_moves_status_without_clobbering_the_target() -> None:
    book, _ = _book()
    book.record("src", "running", source=StatusSource.PTY)
    book.transfer("src", "dst")
    moved = book.current("dst")
    assert moved is not None and moved.status == "running"
    assert book.current("src") is None

    book.record("src2", "running", source=StatusSource.PTY)
    book.record("dst2", "idle", source=StatusSource.RELAY)
    book.transfer("src2", "dst2")
    kept = book.current("dst2")
    assert kept is not None and kept.status == "idle"


def test_status_view_is_live_and_read_only() -> None:
    book, _ = _book()
    view = book.status_view()
    assert isinstance(view, Mapping)
    assert "conv" not in view
    book.record("conv", "running", source=StatusSource.RUNNER)
    assert view["conv"] == "running"
    assert view.get("conv") == "running"
    assert list(view) == ["conv"]
    with pytest.raises(TypeError):
        view["conv"] = "idle"  # type: ignore[index]


def test_dispatch_and_control_stamps() -> None:
    book, clock = _book()
    book.record("conv", "running", source=StatusSource.RUNNER)
    clock.now += 1
    book.record("conv", "idle", source=StatusSource.CONTROL)
    assert book.last_dispatch_at("conv") == 1000.0
    assert book.last_control_idle_at("conv") == 1001.0


def test_a_marked_reset_spares_an_edge_recorded_after_the_mark() -> None:
    book, _ = _book()
    book.record("conv", "running", source=StatusSource.PTY)
    mark = book.edge_mark("conv")
    # Any edge moves the mark, a duplicate from another channel included.
    book.record("conv", "running", source=StatusSource.RELAY)
    assert book.edge_mark("conv") is not mark

    book.reset("conv", "native_terminal_exited", mark=mark)
    record = book.current("conv")
    assert record is not None and record.status == "running"

    book.reset("conv", "native_terminal_exited", mark=book.edge_mark("conv"))
    assert book.current("conv") is None
    # An edge that opens a record after an empty mark is spared too.
    mark = book.edge_mark("conv")
    book.record("conv", "running", source=StatusSource.RUNNER)
    book.reset("conv", "native_terminal_exited", mark=mark)
    assert book.current("conv") is not None


def test_concurrent_writers_keep_seq_monotonic() -> None:
    book = SessionStatusBook()
    seen: list[int] = []
    lock = threading.Lock()

    def _writer(index: int) -> None:
        for step in range(200):
            status = "running" if (step + index) % 2 else "idle"
            book.record("conv", status, source=StatusSource.PTY)
            record = book.current("conv")
            assert record is not None
            with lock:
                seen.append(record.seq)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = book.current("conv")
    assert final is not None
    assert final.seq == max(seen)
    assert final.seq <= 8 * 200


_EDGES: dict[str, tuple[str, StatusSource]] = {
    "runner_running": ("running", StatusSource.RUNNER),
    "pty_running": ("running", StatusSource.PTY),
    "dup_relay_running": ("running", StatusSource.RELAY),
    "relay_idle": ("idle", StatusSource.RELAY),
    "pty_idle": ("idle", StatusSource.PTY),
    "late_relay_running": ("running", StatusSource.RELAY),
    "control_idle": ("idle", StatusSource.CONTROL),
    "reconcile_idle": ("idle", StatusSource.RECONCILE),
}


def test_every_ordering_ends_claims_after_idle() -> None:
    """Over every ordering of eight edges, no stale claim survives an idle."""
    for order in itertools.permutations(_EDGES):
        book, clock = _book()
        local_running_since_idle = False
        for name in order:
            status, source = _EDGES[name]
            previous = book.current("conv")
            clock.now += 1
            book.record("conv", status, source=source)
            current = book.current("conv")
            assert current is not None
            if previous is not None and previous.status == status:
                assert current.since == previous.since, order
            if status == "idle":
                local_running_since_idle = False
            elif source in LOCAL_SOURCES:
                local_running_since_idle = True
            if not local_running_since_idle and status == "idle":
                assert book.claim("conv") is None, order
        if not local_running_since_idle:
            assert book.claim("conv") is None, order


def test_every_record_emits_one_debug_edge_event(caplog: pytest.LogCaptureFixture) -> None:
    book, _ = _book()
    with caplog.at_level("DEBUG", logger="omnigent.runner.session_status"):
        book.record("conv", "running", source=StatusSource.PTY)
        book.record("conv", "running", source=StatusSource.RELAY)
        book.record("conv", "running", source=StatusSource.RELAY, blocked_on="permission")
        book.record("conv", "idle", source=StatusSource.STATUS_FILE)
    edges = [
        (rec.session_id, rec.attributes)
        for rec in caplog.records
        if rec.__dict__.get("event_name") == "session_status_edge"
    ]
    assert edges == [
        (
            "conv",
            {"source": "pty", "status": "running", "changed": True, "blocked_on": None},
        ),
        (
            "conv",
            {"source": "relay", "status": "running", "changed": False, "blocked_on": None},
        ),
        (
            "conv",
            {"source": "relay", "status": "running", "changed": True, "blocked_on": "permission"},
        ),
        (
            "conv",
            {"source": "status_file", "status": "idle", "changed": True, "blocked_on": None},
        ),
    ]


def test_response_id_is_kept_for_diagnostics_only() -> None:
    book, clock = _book()
    book.record("conv", "running", source=StatusSource.RELAY, response_id="codex_turn_1")
    clock.now += 10
    # A re-assert under a new response id neither opens an episode nor fences it.
    assert book.record("conv", "running", source=StatusSource.RELAY, response_id="x") is False
    record = book.current("conv")
    assert record is not None
    assert record.response_id == "x"
    assert record.since == 1000.0
