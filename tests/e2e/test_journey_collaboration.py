"""E2E test: share-and-collaborate user journey.

Exercises the full collaboration lifecycle between two users (Alice and
Bob) against the real server with header auth:

1. Alice creates a session and works with the agent.
2. Alice shares the session with Bob (EDIT level).
3. Bob views conversation history and adds a review comment.
4. Alice sees Bob's comment and asks the agent to address it.
5. Alice downgrades Bob to read-only, then revokes access entirely.

Each permission transition is verified with the expected HTTP status
codes: 403 for insufficient permissions and 404 for revoked access
(anti-enumeration policy).

Usage::

    pytest tests/e2e/test_journey_collaboration.py \
        --llm-api-key $LLM_API_KEY --profile <name> -v
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)

_LEVEL_READ = 1
_LEVEL_EDIT = 2
_LEVEL_MANAGE = 3


def _extract_all_text(body: dict) -> str:  # type: ignore[type-arg]
    """Concatenate all message text blocks from a terminal turn body."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _client_for(base_url: str, email: str) -> httpx.Client:
    """An httpx client authenticated as *email* via header identity.

    :param base_url: The live server base URL.
    :param email: Identity for ``X-Forwarded-Email``.
    """
    return httpx.Client(
        base_url=base_url,
        headers={"X-Forwarded-Email": email},
        timeout=300,
    )


@pytest.mark.llm_flaky(reruns=2)
def test_share_collaborate_revoke_journey(
    live_server: str,
    live_runner_id: str,
    request: pytest.FixtureRequest,
) -> None:
    """Full collaboration lifecycle: share, comment, address, downgrade, revoke."""
    suffix = uuid.uuid4().hex[:6]
    alice_email = f"alice-{suffix}@e2e.test"
    bob_email = f"bob-{suffix}@e2e.test"

    # The live runner is owned by the headerless ``local`` identity, so the
    # session must also be created by a headerless client to satisfy the
    # runner-ownership rule.  Alice and Bob get access via explicit grants.
    owner = httpx.Client(base_url=live_server, timeout=300)
    alice = _client_for(live_server, alice_email)
    bob = _client_for(live_server, bob_email)

    try:
        # ── 1. Owner creates a session and grants Alice MANAGE ─────────
        agent_name = register_inline_agent(
            owner,
            name=f"collab-journey-{suffix}",
            harness="openai-agents",
            model="databricks-gpt-5-4-mini",
            profile=request.config.getoption("--profile"),
            prompt=(
                "You are a terse assistant. Follow instructions exactly. "
                "When asked to address comments, call list_comments to see them, "
                "then call update_comment for each one setting status to 'addressed'."
            ),
        )
        session_id = create_runner_bound_session(
            owner, agent_name=agent_name, runner_id=live_runner_id
        )
        owner.put(
            f"/v1/sessions/{session_id}/permissions",
            json={"user_id": alice_email, "level": _LEVEL_MANAGE},
        ).raise_for_status()

        # ── 2. Alice works with the agent ───────────────────────────────
        marker = f"collab-marker-{uuid.uuid4().hex[:8]}"
        response_id = send_user_message_to_session(
            alice,
            session_id=session_id,
            content=f"Reply with exactly this token and nothing else: {marker}",
        )
        body = poll_session_until_terminal(alice, session_id=session_id, response_id=response_id)
        assert body["status"] == "completed", f"Alice's turn failed: {body.get('error')}"
        assert marker in _extract_all_text(body), (
            f"Marker {marker!r} not found in assistant output"
        )

        # ── 3. Alice shares with Bob (EDIT) ─────────────────────────────
        grant = alice.put(
            f"/v1/sessions/{session_id}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_EDIT},
        )
        assert grant.status_code == 200

        # ── 4. Bob sees the session and Alice's conversation ────────────
        snap = bob.get(f"/v1/sessions/{session_id}")
        assert snap.status_code == 200
        items_text = str(snap.json().get("items", []))
        assert marker in items_text, (
            f"Bob cannot see Alice's conversation marker {marker!r} in session items"
        )

        # ── 5. Bob adds a review comment ────────────────────────────────
        comment_body = f"Review comment from Bob ({suffix})"
        r = bob.post(
            f"/v1/sessions/{session_id}/comments",
            json={
                "path": "app.py",
                "body": comment_body,
                "start_index": 0,
                "end_index": 10,
                "anchor_content": "placeholder",
            },
        )
        r.raise_for_status()
        comment_id: str = r.json()["id"]

        # ── 6. Alice sees Bob's comment ─────────────────────────────────
        comments_resp = alice.get(f"/v1/sessions/{session_id}/comments")
        comments_resp.raise_for_status()
        comments = comments_resp.json()
        bob_comments = [c for c in comments if c["id"] == comment_id]
        assert len(bob_comments) == 1, f"Alice cannot see Bob's comment {comment_id}"
        assert bob_comments[0]["created_by"] == bob_email
        assert bob_comments[0]["status"] == "draft"

        # ── 7. Alice asks the agent to address the comment ──────────────
        response_id = send_user_message_to_session(
            alice,
            session_id=session_id,
            content=(
                "I left a review comment on app.py. "
                "Please do the following steps in order:\n"
                "1. Call list_comments to see the open comments on app.py.\n"
                "2. Call update_comment for each comment, setting status to 'addressed'.\n"
                "3. Confirm you addressed all comments."
            ),
        )
        body = poll_session_until_terminal(
            alice, session_id=session_id, response_id=response_id, timeout=120
        )
        assert body["status"] == "completed", f"Agent turn failed: {body.get('error')}"

        # ── 8. Verify comment is addressed ──────────────────────────────
        comments_resp = alice.get(f"/v1/sessions/{session_id}/comments")
        comments_resp.raise_for_status()
        statuses = {c["id"]: c["status"] for c in comments_resp.json()}
        assert statuses.get(comment_id) == "addressed", (
            f"Comment {comment_id} status is {statuses.get(comment_id)!r}, expected 'addressed'"
        )

        # ── 9. Alice downgrades Bob to read-only ────────────────────────
        downgrade = alice.put(
            f"/v1/sessions/{session_id}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_READ},
        )
        assert downgrade.status_code == 200

        # ── 10. Bob can still read ─────────────────────────────────────
        snap = bob.get(f"/v1/sessions/{session_id}")
        assert snap.status_code == 200
        assert snap.json()["permission_level"] == _LEVEL_READ

        # ── 11. Bob cannot write (post a comment) ──────────────────────
        write_attempt = bob.post(
            f"/v1/sessions/{session_id}/comments",
            json={
                "path": "app.py",
                "body": "This should fail",
                "start_index": 0,
                "end_index": 5,
                "anchor_content": "placeholder",
            },
        )
        assert write_attempt.status_code == 403

        # ── 12. Alice revokes Bob's access ─────────────────────────────
        revoke = alice.delete(f"/v1/sessions/{session_id}/permissions/{bob_email}")
        assert revoke.status_code == 204

        # ── 13. Bob cannot see the session ─────────────────────────────
        assert bob.get(f"/v1/sessions/{session_id}").status_code == 404

    finally:
        owner.close()
        alice.close()
        bob.close()
