"""Three-turn context retention on the harness under test.

The invariant: history established in earlier turns reaches the model
on later dispatches. Each turn is a separate session dispatch through
the harness subprocess, so this fails if the harness loses or fails to
replay its transcript between turns. Runs with either a real LLM or
the mock LLM server.
"""

from __future__ import annotations

import uuid

import httpx

from tests.e2e.conftest import configure_mock_llm
from tests.integration.conftest import JourneySession
from tests.integration.helpers import all_message_text, failure_detail, run_turn


def test_three_turn_context_retention(
    http_client: httpx.Client,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    token_a = f"TOKEN-A-{uuid.uuid4().hex[:8]}"
    token_b = f"TOKEN-B-{uuid.uuid4().hex[:8]}"
    sid = journey_session.session_id

    # Configure all 3 turns up front: the mock server serves them
    # sequentially. With a real LLM the configure call is a no-op.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "ok"},
            {"text": "ok"},
            {"text": f"{token_a} {token_b}"},
        ],
    )

    body_1 = run_turn(
        http_client,
        session_id=sid,
        content=f"Remember this: {token_a}. Reply with just 'ok'.",
    )
    assert body_1["status"] == "completed", f"turn 1 failed: {failure_detail(body_1)}"

    body_2 = run_turn(
        http_client,
        session_id=sid,
        content=f"Also remember this: {token_b}. Reply with just 'ok'.",
    )
    assert body_2["status"] == "completed", f"turn 2 failed: {failure_detail(body_2)}"

    body_3 = run_turn(
        http_client,
        session_id=sid,
        content=(
            "Reply with the two tokens I asked you to remember, "
            "exactly as given, and nothing else."
        ),
    )
    assert body_3["status"] == "completed", f"turn 3 failed: {failure_detail(body_3)}"
    text_3 = all_message_text(body_3)
    # Both literal tokens must surface in turn 3: token A proves
    # turn-1 history survived TWO later dispatches, token B proves the
    # most recent turn did. A missing token means the harness dropped
    # or truncated its transcript between dispatches.
    assert token_a in text_3, f"turn 1 context lost; turn 3: {failure_detail(body_3)}"
    assert token_b in text_3, f"turn 2 context lost; turn 3: {failure_detail(body_3)}"
