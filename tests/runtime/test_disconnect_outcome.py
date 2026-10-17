"""Outcome tracking for turns failed by a runner disconnect."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation
from omnigent.runtime import disconnect_outcome
from tests.debug_log_helpers import capture_debug_rows


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    disconnect_outcome.reset_for_tests()
    yield
    disconnect_outcome.reset_for_tests()


def _outcomes(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [r for r in rows if r["event_name"] == "session_turn_failure_outcome"]


def _arm(session_id: str) -> SessionObservation:
    owner = SessionObservation(
        runner_id="runner-A",
        binding_epoch=1,
        revision=1,
        observation_id="owner-A",
        status="running",
        response_id="turn-A",
    )
    recovered = owner.model_copy(
        update={
            "revision": 2,
            "observation_id": "recovered-A",
            "status": "idle",
            "response_id": None,
        }
    )
    decision = RunnerOfflineDecision(
        observation=owner,
        episode_id="failure-A",
        error_labels={"omnigent.last_task_error_code": "runner_disconnected"},
    )
    disconnect_outcome.note_disconnect_failure_cleared(
        session_id,
        failure_code="runner_disconnected",
        decision=decision,
        recovered_observation=recovered,
    )
    return owner.model_copy(update={"revision": 3, "observation_id": "resumed-A"})


def _owned(event: dict[str, object], owner: SessionObservation) -> dict[str, object]:
    if event.get("type") == "session.status":
        owner = owner.model_copy(update={"status": event["status"]})
    if event.get("type") == "response.output_item.done":
        event = {
            **event,
            "item": {
                "type": "function_call",
                "call_id": "call-A",
                "name": "check",
                "arguments": "{}",
            },
        }
    return {**event, "response_id": owner.response_id, "session_observation": owner.model_dump()}


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
        owner = _arm("conv_a")
        disconnect_outcome.observe_event("conv_a", _owned(event, owner))
        disconnect_outcome.observe_event("conv_a", _owned(event, owner))

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
    owner = _arm("conv_a")
    with capture_debug_rows("server") as rows:
        disconnect_outcome.observe_event("conv_a", _owned(event, owner))
        disconnect_outcome.observe_event(
            "conv_a", _owned({"type": "response.output_text.delta", "delta": "x"}, owner)
        )
    assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["turn_resumed"]


def test_user_turn_first_records_superseded_and_stops_watching() -> None:
    owner = _arm("conv_a")
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_a")
        disconnect_outcome.observe_event(
            "conv_a", _owned({"type": "response.output_text.delta", "delta": "new turn"}, owner)
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
    owner = _arm("conv_a")
    now[0] += disconnect_outcome._WATCH_TTL_S + 1
    with capture_debug_rows("server") as rows:
        disconnect_outcome.observe_event(
            "conv_a", _owned({"type": "session.status", "status": "running"}, owner)
        )
    assert _outcomes(rows) == []
