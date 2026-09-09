"""Session snapshot carries in-flight assistant text (#4984).

A turn parked on an elicitation (AskUserQuestion, permission prompt) has not
settled: its transcript records are withheld and the durable items do not
exist yet, while the live preview that carried the text is transient and
dropped on the refetch the elicitation card triggers. The snapshot must
replay the streamed-so-far text so the card renders with its antecedent
prose before the user answers.
"""

from __future__ import annotations

import pytest

from omnigent.runtime import inflight_text


@pytest.fixture(autouse=True)
def _clean_inflight():
    inflight_text.reset_for_tests() if hasattr(inflight_text, "reset_for_tests") else None
    yield
    inflight_text.reset_for_tests() if hasattr(inflight_text, "reset_for_tests") else None


def test_inflight_snapshot_replays_native_message_text() -> None:
    """A native (message-scoped) streamed buffer survives snapshot_for."""
    conversation_id = "conv_inflight_test"
    inflight_text.record_publish(
        conversation_id,
        {
            "type": "response.output_text.delta",
            "delta": "Both tracks you asked for, ",
            "message_id": "msg_1",
            "index": 0,
            "final": False,
        },
    )
    inflight_text.record_publish(
        conversation_id,
        {
            "type": "response.output_text.delta",
            "delta": "with a decision point.",
            "message_id": "msg_1",
            "index": 1,
            "final": True,
        },
    )
    events = inflight_text.snapshot_for(conversation_id)
    assert events, "streamed text must replay"
    text = "".join(
        e.get("delta", "") for e in events if e.get("type") == "response.output_text.delta"
    )
    assert "Both tracks you asked for" in text
    assert "decision point" in text


async def test_session_response_carries_inflight_text_events(
    client,  # noqa: ANN001 — fixture from the integration conftest
) -> None:
    """The GET snapshot exposes inflight_text_events alongside pending_elicitations."""
    from tests.server.integration.test_sessions_endpoints import (
        _create_session,
        _wait_for_idle,
        create_test_agent,
    )

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], initial_message="hello inflight")
    await _wait_for_idle(client, session["id"])

    # Simulate the parked-turn state: text streamed, elicitation open.
    inflight_text.record_publish(
        session["id"],
        {
            "type": "response.output_text.delta",
            "delta": "Summary the TUI already shows.",
            "message_id": "msg_park",
            "index": 0,
            "final": True,
        },
    )

    resp = await client.get(f"/v1/sessions/{session['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert "inflight_text_events" in body, (
        f"snapshot must include inflight_text_events; got keys {sorted(body.keys())}"
    )
    deltas = [
        e.get("delta", "")
        for e in body["inflight_text_events"]
        if e.get("type") == "response.output_text.delta"
    ]
    assert any("Summary the TUI already shows" in d for d in deltas), deltas
