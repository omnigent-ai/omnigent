"""Outcome tracking for turns failed by a runner disconnect."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.runtime import disconnect_outcome
from tests.debug_log_helpers import capture_debug_rows


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    disconnect_outcome.reset_for_tests()
    yield
    disconnect_outcome.reset_for_tests()


def _outcomes(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [r for r in rows if r["event_name"] == "session_turn_failure_outcome"]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_text.delta", "delta": "still working"},
        {"type": "response.reasoning_text.delta", "delta": "thinking"},
        {"type": "response.function_call_output.delta", "delta": "ok"},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "session.status", "status": "running"},
        {"type": "session.status", "status": "waiting"},
    ],
    ids=lambda e: f"{e['type']}:{e.get('status', '')}",
)
def test_progress_before_a_user_turn_records_resumed(event: dict[str, object]) -> None:
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_disconnect_failure_cleared(
            "conv_a", failure_code="runner_disconnected"
        )
        disconnect_outcome.observe_event("conv_a", event)
        disconnect_outcome.observe_event("conv_a", event)

    [outcome] = _outcomes(rows)
    assert outcome["level"] == "INFO"
    assert outcome["session_id"] == "conv_a"
    assert outcome["attributes"]["outcome"] == "turn_resumed"
    assert outcome["attributes"]["failure_code"] == "runner_disconnected"
    assert outcome["attributes"]["signal"] == event["type"]
    assert any(r["event_name"] == "session_turn_failure_cleared" for r in rows)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "session.status", "status": "idle"},
        {"type": "session.status", "status": "failed"},
        {"type": "response.output_text.delta", "delta": "done", "final": True},
        {"type": "session.usage"},
        {"type": "session.presence"},
    ],
)
def test_non_progress_events_keep_watching(event: dict[str, object]) -> None:
    disconnect_outcome.note_disconnect_failure_cleared(
        "conv_a", failure_code="runner_disconnected"
    )
    with capture_debug_rows("server") as rows:
        disconnect_outcome.observe_event("conv_a", event)
        disconnect_outcome.observe_event(
            "conv_a", {"type": "response.output_text.delta", "delta": "x"}
        )
    assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["turn_resumed"]


def test_user_turn_first_records_superseded_and_stops_watching() -> None:
    disconnect_outcome.note_disconnect_failure_cleared(
        "conv_a", failure_code="runner_disconnected"
    )
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_a")
        disconnect_outcome.observe_event(
            "conv_a", {"type": "response.output_text.delta", "delta": "new turn"}
        )
    assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["superseded_by_user_turn"]


def test_unwatched_sessions_log_nothing() -> None:
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_b")
        disconnect_outcome.observe_event(
            "conv_b", {"type": "response.output_text.delta", "delta": "x"}
        )
    assert _outcomes(rows) == []


def test_resume_after_the_watch_window_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(disconnect_outcome.time, "monotonic", lambda: now[0])
    disconnect_outcome.note_disconnect_failure_cleared(
        "conv_a", failure_code="runner_disconnected"
    )
    now[0] += disconnect_outcome._WATCH_TTL_S + 1
    with capture_debug_rows("server") as rows:
        disconnect_outcome.observe_event("conv_a", {"type": "session.status", "status": "running"})
    assert _outcomes(rows) == []
