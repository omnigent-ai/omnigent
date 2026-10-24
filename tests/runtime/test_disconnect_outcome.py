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


def _watch() -> disconnect_outcome.Failure:
    failure = disconnect_outcome.note_disconnect_failure(
        "conv_a", runner_id="runner-a", response_id="turn-a"
    )
    assert failure is not None
    disconnect_outcome.note_disconnect_failure_cleared(
        "conv_a", failure_code="runner_disconnected", runner_id="runner-a", failure=failure
    )
    return failure


def _observe(session_id: str, event: dict[str, object]) -> None:
    disconnect_outcome.observe_event(session_id, event, runner_id="runner-a", response_id="turn-a")


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_text.delta", "delta": "still working"},
        {"type": "response.reasoning_text.delta", "delta": "thinking"},
        {"type": "response.function_call_output.delta", "delta": "ok"},
        {"type": "session.status", "status": "running"},
        {"type": "session.status", "status": "waiting"},
    ],
    ids=lambda e: f"{e['type']}:{e.get('status', '')}",
)
def test_progress_before_a_user_turn_records_resumed(event: dict[str, object]) -> None:
    with capture_debug_rows("server") as rows:
        _watch()
        _observe("conv_a", event)
        _observe("conv_a", event)

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
        {"type": "response.output_text.delta", "delta": ""},
        {"type": "response.output_text.delta", "delta": "   "},
        {"type": "response.output_item.done", "item": {"type": "error"}},
        {"type": "response.output_item.done", "item": {"type": "message", "role": "user"}},
        {"type": "session.presence"},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "response.output_item.done", "item": {"type": "function_call_output"}},
        {"type": "session.status", "status": "running", "error": {"code": "failed"}},
        {"type": "response.output_text.delta", "delta": "x", "response_id": "other-turn"},
        {"type": "response.output_text.delta", "delta": "x", "response": {"id": "other-turn"}},
        {
            "type": "response.output_text.delta",
            "delta": "x",
            "item": {"response_id": "other-turn"},
        },
    ],
)
def test_non_progress_events_keep_watching(event: dict[str, object]) -> None:
    _watch()
    with capture_debug_rows("server") as rows:
        _observe("conv_a", event)
        _observe("conv_a", {"type": "response.output_text.delta", "delta": "x"})
    assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["turn_resumed"]


def test_user_turn_first_records_superseded_and_stops_watching() -> None:
    _watch()
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_a")
        _observe("conv_a", {"type": "response.output_text.delta", "delta": "new turn"})
    assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["superseded_by_user_turn"]


def test_unwatched_sessions_log_nothing() -> None:
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_b")
        _observe("conv_b", {"type": "response.output_text.delta", "delta": "x"})
    assert _outcomes(rows) == []


@pytest.mark.parametrize("recovery_delay", [0, disconnect_outcome._WATCH_TTL_S - 1])
def test_resume_after_the_watch_window_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, recovery_delay: float
) -> None:
    now = [1000.0]
    monkeypatch.setattr(disconnect_outcome.time, "monotonic", lambda: now[0])
    failure = disconnect_outcome.note_disconnect_failure(
        "conv_a", runner_id="runner-a", response_id="turn-a"
    )
    now[0] += recovery_delay
    disconnect_outcome.note_disconnect_failure_cleared(
        "conv_a", failure_code="runner_disconnected", runner_id="runner-a", failure=failure
    )
    now[0] = 1000.0 + disconnect_outcome._WATCH_TTL_S + 1
    with capture_debug_rows("server") as rows:
        _observe("conv_a", {"type": "session.status", "status": "running"})
    assert _outcomes(rows) == []


@pytest.mark.parametrize(
    "runner_id,response_id",
    [(None, "turn-a"), ("runner-b", "turn-a"), ("runner-a", None), ("runner-a", "turn-b")],
)
def test_unrelated_progress_cannot_resolve_watch(
    runner_id: str | None, response_id: str | None
) -> None:
    failure = _watch()
    with capture_debug_rows("server") as rows:
        disconnect_outcome.observe_event(
            "conv_a",
            {"type": "response.output_text.delta", "delta": "x"},
            runner_id=runner_id,
            response_id=response_id,
        )
        assert not _outcomes(rows)
        _observe("conv_a", {"type": "response.output_text.delta", "delta": "owned"})
    [row] = _outcomes(rows)
    assert row["attributes"]["disconnect_episode_id"] == failure.episode_id
    assert row["attributes"]["runner_id"] == "runner-a"
    assert row["turn_id"] == "turn-a"


@pytest.mark.parametrize("replacement", ["user_turn", "failure"])
def test_delayed_registration_cannot_replace_a_newer_episode(replacement: str) -> None:
    old = _watch()
    if replacement == "user_turn":
        disconnect_outcome.note_user_turn("conv_a")
    else:
        disconnect_outcome.note_disconnect_failure(
            "conv_a", runner_id="runner-a", response_id="turn-b"
        )
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_disconnect_failure_cleared(
            "conv_a", failure_code="runner_disconnected", runner_id="runner-a", failure=old
        )
        _observe("conv_a", {"type": "response.output_text.delta", "delta": "late"})
    assert not _outcomes(rows)


@pytest.mark.parametrize("identity", [None, (None, "turn-a"), ("runner-a", None)])
def test_unidentified_recovery_does_not_install_a_watch(
    identity: tuple[str | None, str | None] | None,
) -> None:
    failure = (
        disconnect_outcome.note_disconnect_failure(
            "conv_a", runner_id=identity[0], response_id=identity[1]
        )
        if identity is not None
        else None
    )
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_disconnect_failure_cleared(
            "conv_a", failure_code="runner_disconnected", failure=failure, runner_id="runner-a"
        )
        _observe("conv_a", {"type": "session.status", "status": "running"})
    assert not _outcomes(rows)


def test_episode_is_scoped_to_workspace() -> None:
    from omnigent.db.db_models import workspace_scope

    with workspace_scope(101):
        _watch()
    with capture_debug_rows("server") as rows:
        with workspace_scope(202):
            _observe("conv_a", {"type": "response.output_text.delta", "delta": "other workspace"})
        assert not _outcomes(rows)
        with workspace_scope(101):
            _observe("conv_a", {"type": "response.output_text.delta", "delta": "owned"})
    assert len(_outcomes(rows)) == 1
