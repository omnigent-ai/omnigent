"""E2E test: "fork and explore alternatives" user journey.

Exercises the realistic workflow of forking a session to explore a
different direction while keeping the original intact:

1. Create a session and build up multi-turn context (two codewords).
2. Fork the session — verify the clone inherits the full history.
3. Continue on the fork with a divergent instruction and verify the
   fork's agent recalls the original context.
4. Verify the original session is untouched by the fork's activity.
5. Delete the fork and confirm the original still works.

This combines the individual fork assertions from
``test_sessions_fork_e2e.py`` into a single end-to-end journey that
mirrors how a user would actually explore alternatives via fork.

Usage::

    pytest tests/e2e/test_journey_fork_explore.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)

# Nonsense codewords the LLM could not guess — must come through
# copied history for the fork's agent to produce them.
_CODEWORD_1 = "aurora-zebra-17"
_CODEWORD_2 = "breeze-falcon-42"


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all output_text blocks from a terminal response body.

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


def _session_item_texts(client: httpx.Client, session_id: str) -> str:
    """Concatenate every text block from a session's items.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :returns: All item text joined by newlines.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    parts: list[str] = []
    for item in resp.json().get("items", []):
        data = item.get("data") or {}
        for block in data.get("content", []) or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


@pytest.mark.llm_flaky(reruns=2)
def test_fork_explore_alternatives_journey(
    http_client: httpx.Client,
    coder_agent: str,
    live_runner_id: str,
) -> None:
    """Fork-and-explore journey: seed, fork, diverge, verify isolation, cleanup.

    Mirrors a real user workflow: build context in a session, fork to
    explore an alternative direction, verify the fork carries history
    and the original is unaffected, then delete the fork and confirm
    the original still works.
    """
    # ── Step 1: Create a runner-bound session ──────────────
    session_id = create_runner_bound_session(
        http_client,
        agent_name=coder_agent,
        runner_id=live_runner_id,
    )

    # ── Step 2: Turn 1 — plant first codeword ─────────────
    resp_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(f"Remember this codeword: {_CODEWORD_1}. Reply with just OK."),
    )
    body_1 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=resp_id_1,
    )
    assert body_1["status"] == "completed", f"turn 1 failed: {body_1.get('error')}"

    # ── Step 3: Turn 2 — plant second codeword ────────────
    resp_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(f"Also remember: {_CODEWORD_2}. Reply with just OK."),
    )
    body_2 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=resp_id_2,
    )
    assert body_2["status"] == "completed", f"turn 2 failed: {body_2.get('error')}"

    # ── Step 4: Fork the session ──────────────────────────
    fork_resp = http_client.post(f"/v1/sessions/{session_id}/fork", json={})
    assert fork_resp.status_code == 201, f"fork failed: {fork_resp.status_code} {fork_resp.text}"
    fork = fork_resp.json()
    fork_id = fork["id"]
    assert fork_id != session_id

    # ── Step 5: Bind runner to the fork ───────────────────
    patch_resp = http_client.patch(
        f"/v1/sessions/{fork_id}",
        json={"runner_id": live_runner_id},
    )
    patch_resp.raise_for_status()

    # ── Step 6: Verify fork carries history from both turns
    fork_text = _session_item_texts(http_client, fork_id)
    assert _CODEWORD_1 in fork_text, (
        f"fork must contain turn-1 codeword, items text: {fork_text!r}"
    )
    assert _CODEWORD_2 in fork_text, (
        f"fork must contain turn-2 codeword, items text: {fork_text!r}"
    )

    # ── Step 7: Continue on the fork — ask for recall ─────
    fork_resp_id = send_user_message_to_session(
        http_client,
        session_id=fork_id,
        content=("List the two codewords I told you earlier. Repeat them exactly as written."),
    )
    fork_body = poll_session_until_terminal(
        http_client,
        session_id=fork_id,
        response_id=fork_resp_id,
    )
    assert fork_body["status"] == "completed", f"fork turn failed: {fork_body.get('error')}"
    fork_reply = _extract_all_text(fork_body)
    assert _CODEWORD_1 in fork_reply, f"fork agent should recall codeword 1, got: {fork_reply!r}"
    assert _CODEWORD_2 in fork_reply, f"fork agent should recall codeword 2, got: {fork_reply!r}"

    # ── Step 8: Verify original is unchanged ──────────────
    original_text = _session_item_texts(http_client, session_id)
    # The fork's recall question must NOT appear in the original.
    assert "list the two codewords" not in original_text.lower(), (
        f"fork activity leaked into original session: {original_text!r}"
    )

    # ── Step 9: Delete the fork ───────────────────────────
    delete_resp = http_client.delete(f"/v1/sessions/{fork_id}")
    assert delete_resp.status_code == 200, (
        f"delete fork failed: {delete_resp.status_code} {delete_resp.text}"
    )

    # ── Step 10: Original still works after fork deletion ─
    final_resp_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Reply with just the word PONG.",
    )
    final_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=final_resp_id,
    )
    assert final_body["status"] == "completed", (
        f"original session broken after fork deletion: {final_body.get('error')}"
    )
