"""Tests for the process-wide server-shutdown signal."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from omnigent.server import shutdown_state


@pytest.fixture(autouse=True)
def _reset_shutdown_state() -> Iterator[None]:
    shutdown_state.reset_for_tests()
    yield
    shutdown_state.reset_for_tests()


def test_unmarked_process_is_not_shutting_down() -> None:
    assert shutdown_state.server_shutting_down() is False


def test_explicit_mark_is_fresh_within_the_window_then_expires() -> None:
    shutdown_state.mark_server_shutting_down()
    assert shutdown_state.server_shutting_down() is True
    expired = time.monotonic() + shutdown_state.SHUTDOWN_WINDOW_S
    assert shutdown_state.server_shutting_down(now=expired) is False


def test_mark_outlasts_the_runner_disconnect_grace() -> None:
    """The relay and the per-runner timer consult the mark only once the grace ends."""
    from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S

    shutdown_state.mark_server_shutting_down()
    decided_at = time.monotonic() + RUNNER_DISCONNECT_GRACE_S + 10.0
    assert shutdown_state.server_shutting_down(now=decided_at) is True


@pytest.mark.parametrize(
    ("code", "expected"),
    [(1012, True), (1000, False), (1001, False), (1006, False), (None, False)],
)
def test_only_a_server_initiated_close_code_marks_shutdown(
    code: int | None, expected: bool
) -> None:
    assert shutdown_state.note_tunnel_close_code(code) is expected
    assert shutdown_state.server_shutting_down() is expected


def test_runner_handoff_is_scoped_to_one_runner_and_expires() -> None:
    shutdown_state.mark_runner_handed_off("runner-a", reason="rebalance")

    assert shutdown_state.runner_handoff_reason("runner-a") == "rebalance"
    assert shutdown_state.runner_handoff_reason("runner-b") is None
    assert shutdown_state.runner_handoff_reason(None) is None
    assert shutdown_state.server_shutting_down() is False
    expired = time.monotonic() + shutdown_state.SHUTDOWN_WINDOW_S
    assert shutdown_state.runner_handoff_reason("runner-a", now=expired) is None


def test_runner_handoff_clears_when_the_runner_reconnects_here() -> None:
    shutdown_state.mark_runner_handed_off("runner-a", reason="rebalance")
    shutdown_state.clear_runner_handoff("runner-a")
    assert shutdown_state.runner_handoff_reason("runner-a") is None


def test_retired_tunnel_marks_a_handoff_without_replacing_an_explicit_reason() -> None:
    shutdown_state.note_runner_tunnel_retired("runner-a")
    assert shutdown_state.runner_handoff_reason("runner-a") == "tunnel_retired"
    assert shutdown_state.server_shutting_down() is False

    shutdown_state.mark_runner_handed_off("runner-b", reason="rebalance")
    shutdown_state.note_runner_tunnel_retired("runner-b")
    assert shutdown_state.runner_handoff_reason("runner-b") == "rebalance"

    # An expired explicit mark no longer describes this retire.
    shutdown_state._handed_off["runner-b"] = (
        time.monotonic() - shutdown_state.SHUTDOWN_WINDOW_S,
        "rebalance",
    )
    shutdown_state.note_runner_tunnel_retired("runner-b")
    assert shutdown_state.runner_handoff_reason("runner-b") == "tunnel_retired"


def test_handoff_and_shutdown_marks_are_logged_for_attribution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=shutdown_state.__name__)

    shutdown_state.mark_runner_handed_off("runner-a", reason="rebalance", slice_key="ws-1")
    shutdown_state.mark_server_shutting_down()
    shutdown_state.note_tunnel_close_code(1012)

    events = {
        getattr(record, "event_name", None): getattr(record, "attributes", None)
        for record in caplog.records
    }
    assert events["runner_tunnel_handed_off"] == {
        "runner_id": "runner-a",
        "handoff_reason": "rebalance",
        "slice_key": "ws-1",
    }
    assert [r.event_name for r in caplog.records if hasattr(r, "event_name")].count(
        "server_shutdown_marked"
    ) == 1
    assert shutdown_state.disconnect_attributes("runner-a") == {
        "server_shutting_down": True,
        "runner_handoff_reason": "rebalance",
    }
