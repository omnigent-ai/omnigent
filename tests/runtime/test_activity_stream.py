"""Tests for the Glitchy activity event side-channel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.runtime import activity_stream, session_stream


@pytest.fixture(autouse=True)
def _clean_activity_stream_state() -> None:
    """Reset process-global stream state around each test."""
    activity_stream.reset_for_tests()
    session_stream._subscribers.clear()
    yield
    activity_stream.close_all()
    activity_stream.reset_for_tests()
    session_stream._subscribers.clear()


async def _collect_activity(
    gen: AsyncIterator[dict[str, Any]],
    expected: int,
) -> list[dict[str, Any]]:
    """Collect exactly ``expected`` activity events."""
    out: list[dict[str, Any]] = []
    try:
        async for event in gen:
            out.append(event)
            if len(out) >= expected:
                return out
        return out
    finally:
        await gen.aclose()


def _register_glitchy_session(session_id: str = "conv_glitchy") -> None:
    activity_stream.register_session_context(
        session_id,
        labels={"omnigent.activity_route": "attention_librarian"},
        agent_name="glitchy-librarian",
        session_title="Glitchy Session",
    )


def test_non_glitchy_session_events_are_ignored() -> None:
    """Session-stream events are ignored until the session is explicitly routed."""
    emitted = activity_stream.record_session_event(
        "conv_regular",
        {"type": "session.status", "status": "running"},
        generated_at="2026-07-07T15:00:00Z",
    )

    assert emitted is None


def test_registered_glitchy_text_delta_is_ignored() -> None:
    """Low-value token deltas do not become standalone activity records."""
    _register_glitchy_session()

    emitted = activity_stream.record_session_event(
        "conv_glitchy",
        {"type": "response.output_text.delta", "delta": "partial"},
        generated_at="2026-07-07T15:00:00Z",
    )

    assert emitted is None


@pytest.mark.asyncio
async def test_session_stream_publish_emits_registered_glitchy_prompt_activity() -> None:
    """The activity stream observes real session-stream publishes."""
    _register_glitchy_session()
    gen = activity_stream.subscribe()
    task = asyncio.create_task(_collect_activity(gen, expected=1))
    await asyncio.sleep(0)

    session_stream.publish(
        "conv_glitchy",
        {
            "type": "session.input.consumed",
            "data": {
                "item_id": "item_1",
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "wire this"}],
                },
            },
        },
    )

    events = await asyncio.wait_for(task, timeout=2.0)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "glitchy.activity"
    assert event["kind"] == "user_message"
    assert event["session_id"] == "conv_glitchy"
    assert event["content_excerpt"] == "wire this"
    assert event["activity_state"] == "flowing"
    assert event["intervention"] == "observe"
    assert "new_prompt_activity" in event["signals"]


def test_repeated_background_runner_failures_are_throttled() -> None:
    """Repeated runner-disconnect incidents become quiet deduped backlog records."""
    _register_glitchy_session("conv_shorts")
    first = activity_stream.record_session_event(
        "conv_shorts",
        {
            "type": "session.status",
            "status": "failed",
            "error": {
                "code": "runner_disconnected",
                "message": "Runner disconnected unexpectedly.",
            },
        },
        generated_at="2026-07-07T15:00:00Z",
    )
    second = activity_stream.record_session_event(
        "conv_shorts",
        {
            "type": "session.status",
            "status": "failed",
            "error": {
                "code": "runner_disconnected",
                "message": "Runner disconnected unexpectedly.",
            },
        },
        generated_at="2026-07-07T15:05:00Z",
    )

    assert first is not None
    assert first["activity_state"] == "background_noise"
    assert first["background"] is True
    assert first["visible"] is False
    assert first["intervention"] == "backlog_candidate"
    assert first["failure_signature"] == "runner_disconnected"
    assert first["error_code"] == "runner_disconnected"
    assert "backlog_candidate" in first["signals"]
    assert "runner_failure" in first["signals"]

    assert second is not None
    assert second["throttled"] is True
    assert second["repeat_count"] == 2
    assert second["intervention"] == "quiet_no_intervention"
    assert second["visible"] is False
    assert "throttled_repeat" in second["signals"]


def test_elicitation_request_classifies_as_needs_choice() -> None:
    """Human choice gates are explicit activity records."""
    _register_glitchy_session()
    event = activity_stream.record_session_event(
        "conv_glitchy",
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_1",
            "params": {"message": "Pick one"},
        },
        generated_at="2026-07-07T15:00:00Z",
    )

    assert event is not None
    assert event["kind"] == "elicitation_request"
    assert event["activity_state"] == "needs_choice"
    assert event["intervention"] == "needs_choice"
    assert event["visible"] is True
    assert "needs_choice" in event["signals"]


def test_repeated_status_ping_classifies_as_frustrated_offer_help() -> None:
    """Repeated user status pings become visible help offers."""
    _register_glitchy_session()
    first = activity_stream.record_activity_event(
        {
            "route": "attention_librarian",
            "kind": "user_message",
            "session_id": "conv_glitchy",
            "session_title": "Glitchy Session",
            "content": "are you there?",
        },
        generated_at="2026-07-07T15:00:00Z",
    )
    second = activity_stream.record_activity_event(
        {
            "route": "attention_librarian",
            "kind": "user_message",
            "session_id": "conv_glitchy",
            "session_title": "Glitchy Session",
            "content": "are you there?",
        },
        generated_at="2026-07-07T15:04:00Z",
    )

    assert first is not None
    assert first["visible"] is False
    assert second is not None
    assert second["activity_state"] == "frustrated"
    assert second["intervention"] == "offer_help"
    assert second["visible"] is True
    assert "abandonment_risk" in second["signals"]
    assert "repeated_status_ping" in second["signals"]
