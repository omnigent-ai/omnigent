"""How a terminal's end is put on the attach close and the deletion event."""

from __future__ import annotations

import pytest

from omnigent.inner.terminal_exit_outcome import TERMINAL_ENDED_REASON, TerminalExitOutcome


@pytest.mark.parametrize(
    ("outcome", "reason", "payload", "definitive"),
    [
        (
            TerminalExitOutcome(status=0, signal=None),
            "terminal session ended; exit=status:0",
            {"status": 0, "signal": None},
            True,
        ),
        (
            TerminalExitOutcome(status=127, signal=None),
            "terminal session ended; exit=status:127",
            {"status": 127, "signal": None},
            True,
        ),
        (
            TerminalExitOutcome(status=None, signal="SIGKILL"),
            "terminal session ended; exit=signal:SIGKILL",
            {"status": None, "signal": "SIGKILL"},
            True,
        ),
        # A signal wins: tmux reports no exit code for a killed process.
        (
            TerminalExitOutcome(status=0, signal="SIGTERM"),
            "terminal session ended; exit=signal:SIGTERM",
            {"status": 0, "signal": "SIGTERM"},
            True,
        ),
        (
            TerminalExitOutcome(status=None, signal=None),
            TERMINAL_ENDED_REASON,
            {"status": None, "signal": None},
            False,
        ),
    ],
)
def test_outcome_wire_forms(
    outcome: TerminalExitOutcome,
    reason: str,
    payload: dict[str, object],
    definitive: bool,
) -> None:
    assert outcome.close_reason() == reason
    assert outcome.event_payload() == payload
    assert outcome.definitive is definitive


@pytest.mark.parametrize(
    ("raw_signal", "name"),
    [
        # macOS tmux 3.5a reports a signal by its lowercase name, without SIG.
        ("kill", "SIGKILL"),
        ("sigkill", "SIGKILL"),
        ("term", "SIGTERM"),
        ("9", "SIGKILL"),
        ("SIGKILL", "SIGKILL"),
    ],
)
def test_tmux_signal_text_is_normalized_to_its_name(raw_signal: str, name: str) -> None:
    outcome = TerminalExitOutcome(status=None, signal=raw_signal)

    assert outcome.close_reason() == f"{TERMINAL_ENDED_REASON}; exit=signal:{name}"
    assert outcome.event_payload() == {"status": None, "signal": name}
    assert outcome.definitive


@pytest.mark.parametrize("raw_signal", ["9 (raw)", "", "999", "SIG" + "X" * 40])
def test_unrecognized_signal_text_never_reaches_the_wire(raw_signal: str) -> None:
    """tmux's raw fallback text isn't a signal name, so the end stays unknown."""
    outcome = TerminalExitOutcome(status=None, signal=raw_signal)

    assert outcome.close_reason() == TERMINAL_ENDED_REASON
    assert outcome.event_payload() == {"status": None, "signal": None}
    assert not outcome.definitive
