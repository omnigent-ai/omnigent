"""Unit tests for ``omnigent.runner.claude_goal``."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.responses import Response

from omnigent.harnesses.claude_native import bridge
from omnigent.runner.claude_goal import CLAUDE_GOAL_GET_EVENT, ClaudeGoalRunner

SESSION = "conv_goal"
GOAL_SET = {
    "type": "attachment",
    "attachment": {
        "type": "goal_status",
        "met": False,
        "sentinel": True,
        "condition": "All tests pass",
    },
}


class _BridgeIds:
    """Resolve bridge ids from a queue of answers or exceptions, recording calls."""

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    async def __call__(self, conv_id: str) -> str:
        self.calls.append(conv_id)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Map bridge ids to transcript paths that ``read_transcript_path`` reports."""
    by_bridge: dict[str, Path] = {}
    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda bridge_id: tmp_path / bridge_id)
    monkeypatch.setattr(
        bridge, "read_transcript_path", lambda bridge_dir: by_bridge.get(bridge_dir.name)
    )
    return by_bridge


def _body(response: Response | None) -> object:
    assert response is not None and response.status_code == 200
    return json.loads(bytes(response.body))


async def _get(runner: ClaudeGoalRunner, harness: str | None = "claude-native") -> object:
    return _body(
        await runner.handle_event(
            SESSION, CLAUDE_GOAL_GET_EVENT, session_harness_name=lambda conv_id: harness
        )
    )


UNAVAILABLE = {"goal": None, "available": False}


async def test_other_events_are_not_handled() -> None:
    runner = ClaudeGoalRunner(bridge_id_for_session=_BridgeIds())

    assert (
        await runner.handle_event(SESSION, "goal_get", session_harness_name=lambda conv_id: None)
        is None
    )


async def test_other_harnesses_are_unavailable_without_a_bridge_lookup() -> None:
    bridge_ids = _BridgeIds()

    assert await _get(ClaudeGoalRunner(bridge_id_for_session=bridge_ids), "codex-native") == (
        UNAVAILABLE
    )
    assert bridge_ids.calls == []


async def test_reads_the_goal_from_the_bridge_transcript(
    tmp_path: Path, transcripts: dict[str, Path]
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    transcripts["bridge_1"] = transcript
    bridge_ids = _BridgeIds("bridge_1")
    runner = ClaudeGoalRunner(bridge_id_for_session=bridge_ids)

    assert await _get(runner) == {"goal": None, "available": True}

    transcript.write_text(json.dumps(GOAL_SET) + "\n")
    body = await _get(runner)

    assert body == {
        "goal": {
            "condition": "All tests pass",
            "status": "active",
            "detail": None,
            "iterations": None,
            "tokens": None,
            "duration_ms": None,
            "set_at": None,
            "updated_at": None,
        },
        "available": True,
    }
    # The labelled bridge id is cached across reads.
    assert bridge_ids.calls == [SESSION]


async def test_unlabelled_or_failed_lookups_are_retried(
    tmp_path: Path, transcripts: dict[str, Path]
) -> None:
    bridge_ids = _BridgeIds(httpx.ConnectError("down"), SESSION, "bridge_1")
    runner = ClaudeGoalRunner(bridge_id_for_session=bridge_ids)

    assert await _get(runner) == UNAVAILABLE
    # No transcript reported for the session-id fallback yet.
    assert await _get(runner) == UNAVAILABLE

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps(GOAL_SET) + "\n")
    transcripts["bridge_1"] = transcript
    body = await _get(runner)

    assert isinstance(body, dict) and body["available"] is True
    assert bridge_ids.calls == [SESSION, SESSION, SESSION]


async def test_an_unreadable_transcript_is_unavailable(
    tmp_path: Path, transcripts: dict[str, Path]
) -> None:
    transcripts["bridge_1"] = tmp_path / "deleted.jsonl"
    runner = ClaudeGoalRunner(bridge_id_for_session=_BridgeIds("bridge_1"))

    assert await _get(runner) == UNAVAILABLE


async def test_forget_drops_the_cached_bridge_id(
    tmp_path: Path, transcripts: dict[str, Path]
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    transcripts["bridge_1"] = transcript
    bridge_ids = _BridgeIds("bridge_1", "bridge_1")
    runner = ClaudeGoalRunner(bridge_id_for_session=bridge_ids)

    await _get(runner)
    runner.forget(SESSION)
    await _get(runner)

    assert bridge_ids.calls == [SESSION, SESSION]
