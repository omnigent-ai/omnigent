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
        {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant"}},
        {"type": "session.status", "status": "running"},
        {"type": "session.status", "status": "waiting"},
    ],
    ids=lambda e: f"{e['type']}:{e.get('status', '')}{e.get('item', {}).get('type', '')}",
)
def test_progress_before_a_user_turn_records_resumed(event: dict[str, object]) -> None:
    with capture_debug_rows("server") as rows:
        disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
        disconnect_outcome.observe_event("conv_a", event)
        disconnect_outcome.observe_event("conv_a", event)

    [outcome] = _outcomes(rows)
    assert outcome["level"] == "INFO"
    assert outcome["session_id"] == "conv_a"
    assert outcome["attributes"]["outcome"] == "turn_resumed"
    assert outcome["attributes"]["failure_code"] == "runner_disconnected"
    assert outcome["attributes"]["signal"] == event["type"]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "session.status", "status": "idle"},
        {"type": "session.status", "status": "failed"},
        {"type": "response.output_text.delta", "delta": "done", "final": True},
        {"type": "session.usage"},
        {"type": "session.presence"},
        # A child's lifecycle notification reaches the parent as an output item.
        {
            "type": "response.output_item.done",
            "item": {"type": "resource_event", "event_type": "session.subagent.returned"},
        },
        # User input mirrored or echoed back is not the agent making progress.
        {"type": "response.output_item.done", "item": {"type": "message", "role": "user"}},
        {"type": "response.output_item.done", "item": {"type": "slash_command"}},
    ],
)
def test_non_progress_events_keep_watching(event: dict[str, object]) -> None:
    disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
    with capture_debug_rows("server") as rows:
        disconnect_outcome.observe_event("conv_a", event)
        disconnect_outcome.observe_event(
            "conv_a", {"type": "response.reasoning_text.delta", "delta": "x"}
        )
    [outcome] = _outcomes(rows)
    assert outcome["attributes"]["outcome"] == "turn_resumed"
    # Only the genuine delta may resolve the watch, never the earlier event.
    assert outcome["attributes"]["signal"] == "response.reasoning_text.delta"


def test_user_turn_first_records_superseded_and_stops_watching() -> None:
    disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_a")
        disconnect_outcome.observe_event(
            "conv_a", {"type": "response.output_text.delta", "delta": "new turn"}
        )
    assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["superseded_by_user_turn"]


@pytest.mark.parametrize("signal", ["turn_running", "turn_completed"])
def test_relay_observed_resume_is_recorded_without_a_watch(signal: str) -> None:
    """The relay seeing the failed turn in flight or finishing is itself the recovery."""
    disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_turn_resumed(
            "conv_a", failure_code="runner_disconnected", signal=signal
        )
        disconnect_outcome.observe_event(
            "conv_a", {"type": "response.output_text.delta", "delta": "late"}
        )
    [outcome] = _outcomes(rows)
    assert outcome["attributes"]["outcome"] == "turn_resumed"
    assert outcome["attributes"]["signal"] == signal


def test_running_edge_right_after_a_user_turn_supersedes_the_failed_turn() -> None:
    """A new turn's running edge is not the failed turn resuming."""
    disconnect_outcome.note_user_turn("conv_a")
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_turn_resumed(
            "conv_a", failure_code="runner_disconnected", signal="turn_running"
        )
    [outcome] = _outcomes(rows)
    assert outcome["attributes"]["outcome"] == "superseded_by_user_turn"
    assert outcome["attributes"]["signal"] == "user_turn"


def test_rearming_a_watch_makes_it_newest_for_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the cap the oldest armed watch goes, not a session that just re-failed."""
    monkeypatch.setattr(disconnect_outcome, "_MAX_WATCHES", 2)
    disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
    disconnect_outcome.watch_for_resume("conv_b", failure_code="runner_disconnected")
    disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
    disconnect_outcome.watch_for_resume("conv_c", failure_code="runner_disconnected")
    with capture_debug_rows("server") as rows:
        for cid in ("conv_a", "conv_b", "conv_c"):
            disconnect_outcome.observe_event(cid, {"type": "session.status", "status": "running"})
    assert sorted(str(o["session_id"]) for o in _outcomes(rows)) == ["conv_a", "conv_c"]


def test_unwatched_sessions_log_nothing() -> None:
    with capture_debug_rows("server") as rows:
        disconnect_outcome.note_user_turn("conv_b")
        disconnect_outcome.observe_event(
            "conv_b", {"type": "response.output_text.delta", "delta": "x"}
        )
    assert _outcomes(rows) == []


@pytest.mark.parametrize("late_signal", ["progress", "user_turn"])
def test_signals_after_the_watch_window_are_not_recorded(
    monkeypatch: pytest.MonkeyPatch, late_signal: str
) -> None:
    """Neither a resume nor a user turn is attributed to a failure older than the window."""
    now = [1000.0]
    monkeypatch.setattr(disconnect_outcome.time, "monotonic", lambda: now[0])
    disconnect_outcome.watch_for_resume("conv_a", failure_code="runner_disconnected")
    now[0] += disconnect_outcome._WATCH_TTL_S + 1
    with capture_debug_rows("server") as rows:
        if late_signal == "user_turn":
            disconnect_outcome.note_user_turn("conv_a")
        else:
            disconnect_outcome.observe_event(
                "conv_a", {"type": "session.status", "status": "running"}
            )
    assert _outcomes(rows) == []


def test_replayed_native_deltas_are_not_progress() -> None:
    """Deltas the stream suppresses as replays of committed text leave the watch armed."""
    from omnigent.runtime import inflight_text, session_stream

    cid = "conv_replay"
    committed = {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": "ci_1",
            "content": [{"type": "output_text", "text": "Hello big world"}],
        },
    }

    def native_delta(index: int, text: str, message_id: str = "m1") -> dict[str, object]:
        return {
            "type": "response.output_text.delta",
            "delta": text,
            "message_id": message_id,
            "index": index,
            "final": False,
        }

    inflight_text.reset_for_tests()
    try:
        # The message committed before the drop; the reconnected runner replays its chunks.
        session_stream.publish(cid, committed)
        disconnect_outcome.watch_for_resume(cid, failure_code="runner_disconnected")
        with capture_debug_rows("server") as rows:
            session_stream.publish(cid, native_delta(0, "Hello "))
            session_stream.publish(cid, native_delta(1, "big "))
            assert _outcomes(rows) == []
            session_stream.publish(cid, native_delta(0, "Next step", message_id="m2"))
        assert [o["attributes"]["outcome"] for o in _outcomes(rows)] == ["turn_resumed"]
    finally:
        inflight_text.reset_for_tests()
        session_stream.close(cid)
