"""E2E test: multi-turn research workflow user journey.

Exercises multi-turn context retention where the agent receives
information in turn 1 and must reference it in turn 2:

1. **Turn 1**: Tell the agent a distinctive fact via a document upload.
2. **Turn 2**: Ask the agent to recall and reason about that fact,
   proving context retention across turns.

Usage::

    pytest tests/e2e/test_journey_web_research.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text


@pytest.mark.llm_flaky(reruns=2)
def test_multi_turn_research_workflow(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """Agent receives facts in turn 1 and reasons about them in turn 2.

    Turn 1: provide the agent with a distinctive fact ("The capital of
    Freedonia is Quuxville, founded in 1847"). Verify the agent
    acknowledges it.

    Turn 2: ask a follow-up requiring the fact from turn 1 ("When was
    the capital of Freedonia founded?"). Verify the agent references
    "1847", proving multi-turn context retention.
    """
    session_id = create_runner_bound_session(
        http_client,
        agent_name=archer_agent,
        runner_id=live_runner_id,
    )

    # ── Turn 1: provide facts ─────────────────────────────
    resp_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Here is an important fact: The capital of Freedonia is "
            "Quuxville, founded in 1847. Please acknowledge that you "
            "have received this information."
        ),
    )
    result_1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=resp_id_1, timeout=120
    )
    text_1 = final_assistant_text(result_1).lower()
    assert "quuxville" in text_1 or "1847" in text_1, (
        f"Turn 1: agent did not acknowledge the fact. Text: {text_1[:500]!r}"
    )

    # ── Turn 2: follow-up requiring context retention ──────
    resp_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="When was the capital of Freedonia founded? Answer with just the year.",
    )
    result_2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=resp_id_2, timeout=120
    )
    text_2 = final_assistant_text(result_2).lower()
    assert "1847" in text_2, (
        "Turn 2 did not reference '1847' from turn 1 — "
        f"context retention failed. Text: {text_2[:500]!r}"
    )
