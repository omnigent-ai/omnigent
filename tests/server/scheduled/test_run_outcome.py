"""Tests for waiting on in-flight fires before recording a run's outcome."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.server.scheduled import run_outcome
from omnigent.server.scheduled.run_outcome import (
    PromptsInFlight,
    RunStillStartingError,
    record_outcome_once_delivered,
)


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record_run_outcome(self, session_id: str, **kwargs: Any) -> str:
        self.calls.append((session_id, kwargs))
        return "run"


def test_nested_marks_clear_only_when_the_last_one_ends() -> None:
    marks = PromptsInFlight()
    with marks.track("s"):
        with marks.track("s"):
            assert marks.in_flight("s")
        assert marks.in_flight("s")
        assert not marks.in_flight("other")
    assert not marks.in_flight("s")


def test_a_failing_fire_still_clears_its_mark() -> None:
    marks = PromptsInFlight()
    with pytest.raises(RuntimeError), marks.track("s"):
        raise RuntimeError("dispatch failed")
    assert not marks.in_flight("s")


@pytest.mark.asyncio
async def test_recording_waits_for_the_fire_then_writes() -> None:
    store = _RecordingStore()
    marks = run_outcome.PROMPTS_IN_FLIGHT
    with marks.track("s"):
        pending = asyncio.create_task(
            record_outcome_once_delivered(
                store,  # type: ignore[arg-type]
                "s",
                success=False,
                emoji="🚧",
                text="Blocked",
                recorded_at=1,
            )
        )
        await asyncio.sleep(0.15)
        assert not pending.done()
        assert store.calls == []
    assert await pending == "run"
    assert store.calls == [
        ("s", {"success": False, "emoji": "🚧", "text": "Blocked", "recorded_at": 1})
    ]


@pytest.mark.asyncio
async def test_recording_gives_up_while_the_fire_is_still_starting() -> None:
    store = _RecordingStore()
    with run_outcome.PROMPTS_IN_FLIGHT.track("s"):
        with pytest.raises(RunStillStartingError):
            await record_outcome_once_delivered(
                store,  # type: ignore[arg-type]
                "s",
                success=True,
                emoji="✅",
                text="Done",
                recorded_at=1,
                timeout_s=0.1,
            )
    assert store.calls == []
