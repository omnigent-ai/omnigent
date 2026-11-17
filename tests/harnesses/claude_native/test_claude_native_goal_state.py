"""Tests for deriving Claude Code's ``/goal`` state from its transcript."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.goal_state import (
    ClaudeGoal,
    ClaudeGoalStateReader,
    apply_goal_entry,
)

CONDITION = "All tests pass"
SET_AT = "2026-01-01T00:00:00.000Z"
SET_AT_UNIX = 1767225600.0


def _goal_status(*, timestamp: str = SET_AT, **attachment: Any) -> dict[str, Any]:
    return {
        "type": "attachment",
        "timestamp": timestamp,
        "attachment": {"type": "goal_status", "condition": CONDITION, **attachment},
    }


def _notice(content: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "informational",
        "content": content,
        "timestamp": "2026-01-01T00:01:00.000Z",
    }


def _user(content: object, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


SET = _goal_status(met=False, sentinel=True)
CLEAR = _goal_status(met=True, sentinel=True)
UNMET = _goal_status(met=False, reason="Two tests still fail")
MET = _goal_status(met=True, reason="12 passed", iterations=3, tokens=1200, durationMs=95000)
FAILED = _goal_status(met=False, failed=True, reason="Evaluator errored")
PAUSED = _notice("Goal paused · goal checks kept finding it unmet this turn")
RETRYING = _notice("Goal still active · the request was rate limited · retrying in 1 min")


def _fold(*entries: dict[str, Any]) -> ClaudeGoal | None:
    goal: ClaudeGoal | None = None
    for entry in entries:
        goal = apply_goal_entry(goal, entry)
    return goal


def test_setting_a_goal_makes_it_active() -> None:
    assert _fold(SET) == ClaudeGoal(
        CONDITION, "active", set_at=SET_AT_UNIX, updated_at=SET_AT_UNIX
    )


def test_an_unmet_check_keeps_the_goal_active_with_its_reason() -> None:
    goal = _fold(SET, UNMET)

    assert goal is not None
    assert (goal.status, goal.detail, goal.set_at) == (
        "active",
        "Two tests still fail",
        SET_AT_UNIX,
    )


def test_a_met_check_completes_the_goal_with_its_usage() -> None:
    goal = _fold(SET, UNMET, MET)

    assert goal is not None
    assert (goal.status, goal.detail) == ("complete", "12 passed")
    assert (goal.iterations, goal.tokens, goal.duration_ms) == (3, 1200, 95000)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ((SET, CLEAR), None),
        ((SET, FAILED), "failed"),
        ((SET, PAUSED), "paused"),
        ((SET, PAUSED, RETRYING), "active"),
        ((SET, PAUSED, _user("keep going")), "active"),
        ((SET, MET, PAUSED), "complete"),
        ((SET, MET, _user("thanks")), None),
        ((SET, FAILED, _user("try again")), None),
        ((SET, PAUSED, _user("Stop hook feedback", isMeta=True)), "paused"),
        ((SET, PAUSED, _user([{"type": "tool_result", "content": "ok"}])), "paused"),
        ((SET, PAUSED, _user([{"type": "text", "text": "continue"}])), "active"),
        ((SET, PAUSED, _user("   ")), "paused"),
        ((SET, PAUSED, _user("<command-name>/cost</command-name>")), "paused"),
        ((SET, PAUSED, _user("<command-message>review</command-message>")), "paused"),
        (
            (SET, PAUSED, _user("<local-command-stdout>Total cost: $0</local-command-stdout>")),
            "paused",
        ),
        ((SET, PAUSED, _user("<bash-input>ls</bash-input>")), "paused"),
        (
            (SET, PAUSED, _user([{"type": "text", "text": "[Request interrupted by user]"}])),
            "paused",
        ),
        ((SET, MET, _user("Summary of the conversation", isCompactSummary=True)), "complete"),
        ((SET, PAUSED, _user("[Request interrupted by user?] keep going")), "active"),
        ((SET, MET, SET), "active"),
        ((_notice("Goal paused · x"), _user("hi")), None),
        ((SET, _notice("Compacting conversation")), "active"),
        ((SET, {**CLEAR, "isSidechain": True}), "active"),
        ((SET, _goal_status(met=True, sentinel=True, condition="")), "active"),
        ((SET, {"type": "attachment", "attachment": {"type": "todo"}}), "active"),
    ],
)
def test_goal_state_transitions(entries: tuple[dict[str, Any], ...], expected: str | None) -> None:
    goal = _fold(*entries)

    assert (goal.status if goal is not None else None) == expected


def test_a_pause_notice_becomes_the_detail_and_a_message_clears_it() -> None:
    paused = _fold(SET, UNMET, PAUSED)
    assert paused is not None and paused.detail == PAUSED["content"]

    resumed = apply_goal_entry(paused, _user("keep going"))

    assert resumed is not None and resumed.detail is None


def _write(path: Path, *entries: dict[str, Any], partial: str = "") -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        handle.write(partial)


def test_reader_folds_only_new_complete_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write(transcript, _user("hello"), {"type": "assistant"}, SET)
    reader = ClaudeGoalStateReader(transcript)

    goal = reader.read()
    assert goal is not None and goal.status == "active"

    met_line = json.dumps(MET, separators=(",", ":"))
    _write(transcript, partial=met_line[:20])
    goal = reader.read()
    assert goal is not None and goal.status == "active"

    _write(transcript, partial=met_line[20:] + "\nnot json\n")
    goal = reader.read()
    assert goal is not None and goal.status == "complete"


def test_reader_starts_over_when_the_transcript_is_replaced_or_truncated(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write(transcript, SET, MET)
    reader = ClaudeGoalStateReader(transcript)
    assert reader.read() is not None

    transcript.write_text("")
    assert reader.read() is None

    replacement = tmp_path / "replacement.jsonl"
    _write(replacement, SET)
    replacement.replace(transcript)
    goal = reader.read()
    assert goal is not None and goal.status == "active"


def test_reader_raises_when_the_transcript_is_missing(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        ClaudeGoalStateReader(tmp_path / "missing.jsonl").read()
