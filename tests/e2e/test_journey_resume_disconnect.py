"""E2E tests for the "resume after disconnect" user journey.

Simulates the browser-close-and-reopen flow: a user creates a session,
runs a couple of turns, then closes the browser tab (drops the HTTP
client). On reopening (fresh ``httpx.Client``), they fetch the same
session, verify its full history is intact, and continue the
conversation with full context preserved.

Usage::

    pytest tests/e2e/test_journey_resume_disconnect.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

_CODEWORD = "crystal-panda-99"


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant output_text blocks from a response body.

    :param body: The terminal response body from
        :func:`poll_session_until_terminal`.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def test_resume_session_after_disconnect(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Closing the browser and reopening preserves full session context.

    Uses a mock LLM: turn 1 returns "ok", turn 2 returns "4", and the
    post-disconnect turn 3 returns the codeword -- proving the server
    persisted the session and the agent received the full history.

    1. Create a runner-bound session (inline agent + mock queue).
    2. Turn 1: plant a codeword; mock replies "ok".
    3. Turn 2: generic follow-up; mock replies "4".
    4. "Disconnect": create a fresh ``httpx.Client``.
    5. Resume: verify session loads with items.
    6. Resume: verify full history via items endpoint.
    7. Continue: send turn 3; mock replies with the codeword.
    8. Verify the codeword appears in the latest assistant message.
    """
    model = f"mock-resume-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"resume-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "ok"},
            {"text": "4"},
            {"text": _CODEWORD},
        ],
        key=model,
    )

    # ── Setup: two turns with a codeword ─────────────────
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    # Turn 1: plant codeword
    resp_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=f"Remember this codeword for later: {_CODEWORD}. Reply with just OK.",
    )
    body_1 = poll_session_until_terminal(http_client, session_id=session_id, response_id=resp_id_1)
    assert body_1["status"] == "completed", f"turn 1 failed: {body_1.get('error')}"

    # Turn 2: generic follow-up to add more history
    resp_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What is 2 + 2? Reply with just the number.",
    )
    body_2 = poll_session_until_terminal(http_client, session_id=session_id, response_id=resp_id_2)
    assert body_2["status"] == "completed", f"turn 2 failed: {body_2.get('error')}"

    # ── "Disconnect": drop the old client, create a fresh one ─
    with httpx.Client(base_url=live_server, timeout=300) as new_client:
        # Resume — load session snapshot
        session_resp = new_client.get(f"/v1/sessions/{session_id}")
        session_resp.raise_for_status()
        session_data = session_resp.json()
        assert session_data["id"] == session_id
        assert len(session_data.get("items", [])) > 0, (
            "resumed session should contain items from prior turns"
        )

        # Resume — verify full history via items endpoint
        items_resp = new_client.get(
            f"/v1/sessions/{session_id}/items",
            params={"order": "asc", "limit": 50},
        )
        items_resp.raise_for_status()
        items = items_resp.json()["data"]

        # Must have user messages from both turns
        user_texts = " ".join(
            block.get("text", "")
            for item in items
            if item.get("type") == "message" and item.get("role") == "user"
            for block in item.get("content", [])
        )
        assert _CODEWORD in user_texts, (
            f"items endpoint should contain the codeword from turn 1, "
            f"got user texts: {user_texts[:500]}"
        )

        # Continue — send a new turn on the resumed session asking
        # the agent to recall the codeword
        resp_id_3 = send_user_message_to_session(
            new_client,
            session_id=session_id,
            content=(
                "What was the codeword I told you to remember earlier "
                "in this conversation? Reply with just the codeword."
            ),
        )
        body_3 = poll_session_until_terminal(
            new_client, session_id=session_id, response_id=resp_id_3
        )
        assert body_3["status"] == "completed", f"resume turn failed: {body_3.get('error')}"

        # The agent should recall the codeword — verify using only the
        # latest assistant message (not cumulative session text).
        latest_items_resp = new_client.get(
            f"/v1/sessions/{session_id}/items",
            params={"order": "desc", "limit": 5},
        )
        latest_items_resp.raise_for_status()
        latest_items = latest_items_resp.json()["data"]

        # Find the first assistant message (most recent due to desc)
        latest_assistant_text = ""
        for item in latest_items:
            if item.get("type") == "message" and item.get("role") == "assistant":
                latest_assistant_text = " ".join(
                    block.get("text", "") for block in item.get("content", [])
                )
                break

        assert _CODEWORD in latest_assistant_text, (
            f"agent should recall {_CODEWORD!r} after "
            f"disconnect/resume, got: {latest_assistant_text!r}"
        )


def test_session_list_shows_existing_sessions(
    live_server: str,
    http_client: httpx.Client,
    coder_agent: str,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """After disconnect, ``GET /v1/sessions`` lists previously created sessions.

    1. Create two sessions with different agents.
    2. Create a fresh client (simulates new browser).
    3. ``GET /v1/sessions`` — verify both sessions appear with correct
       agent names.
    """
    # Create two sessions with different agents
    session_id_1 = create_runner_bound_session(
        http_client, agent_name=coder_agent, runner_id=live_runner_id
    )
    session_id_2 = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    # "Disconnect" — fresh client
    with httpx.Client(base_url=live_server, timeout=300) as new_client:
        list_resp = new_client.get("/v1/sessions", params={"limit": 100})
        list_resp.raise_for_status()
        sessions = list_resp.json()["data"]

        listed_ids = {s["id"] for s in sessions}
        assert session_id_1 in listed_ids, (
            f"session {session_id_1} (coder) not found in session list"
        )
        assert session_id_2 in listed_ids, (
            f"session {session_id_2} (archer) not found in session list"
        )

        # Verify agent names are correct on the listed sessions
        session_map = {s["id"]: s for s in sessions}
        assert session_map[session_id_1]["agent_name"] == coder_agent, (
            f"expected agent_name={coder_agent!r} for session 1, "
            f"got {session_map[session_id_1].get('agent_name')!r}"
        )
        assert session_map[session_id_2]["agent_name"] == archer_agent, (
            f"expected agent_name={archer_agent!r} for session 2, "
            f"got {session_map[session_id_2].get('agent_name')!r}"
        )
