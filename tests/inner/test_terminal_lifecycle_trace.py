"""Tests for the per-launch terminal lifecycle trace."""

from __future__ import annotations

from omnigent.inner.terminal_lifecycle import TerminalLifecycleTrace


def test_input_ready_is_unset_until_the_tui_accepts_input() -> None:
    trace = TerminalLifecycleTrace(session_id="conv_a")

    assert trace.snapshot()["terminal_input_ready_at"] is None
    assert "terminal_input_ready_at" not in trace.log_attributes()


def test_input_ready_keeps_the_first_observation() -> None:
    trace = TerminalLifecycleTrace(session_id="conv_a")

    trace.note_input_ready()
    first = trace.snapshot()["terminal_input_ready_at"]
    trace.note_input_ready()

    assert isinstance(first, float)
    assert trace.snapshot()["terminal_input_ready_at"] == first
    assert trace.log_attributes()["terminal_input_ready_at"] == str(first)


def test_a_new_launch_starts_not_ready() -> None:
    trace = TerminalLifecycleTrace(session_id="conv_a")
    trace.note_input_ready()

    trace.launch_environment("a" * 32)

    assert trace.snapshot()["terminal_input_ready_at"] is None
