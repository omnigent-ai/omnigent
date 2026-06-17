"""Single-turn marker echo: the cheapest per-harness liveness signal.

Kept separate from the multi-turn journeys so the nightly leg still
reports basic harness health even when a longer journey is red for
content reasons. Runs with either a real LLM or the mock LLM server.
"""

from __future__ import annotations

import uuid

import httpx

from tests.e2e.conftest import configure_mock_llm
from tests.integration.conftest import JourneySession
from tests.integration.helpers import all_message_text, failure_detail, run_turn


def test_single_turn_marker_echo(
    http_client: httpx.Client,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    marker = f"SMOKE-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_llm_server_url, [{"text": marker}])
    body = run_turn(
        http_client,
        session_id=journey_session.session_id,
        content=f"Reply with exactly this token and nothing else: {marker}",
    )
    assert body["status"] == "completed", f"turn failed: {failure_detail(body)}"
    # The literal marker proves the prompt round-tripped through the
    # harness subprocess and the gateway, not just that SOME text came
    # back (a harness that drops the user message still produces text).
    assert marker in all_message_text(body)
