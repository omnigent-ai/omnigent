"""Cross-user sharing on the harness under test.

The runner-owning ``local`` identity owns a session, establishes
context, and grants Bob EDIT; Bob's follow-up turn must see the
owner's context AND complete under Bob's identity. (The owner must be
``local``: the runner-ownership rule forbids binding a session to
another user's runner, and the fixture runner is registered
headerless.) HTTP-level permission semantics live in
``tests/e2e/test_sharing_permissions_e2e.py``; this journey pins the
per-harness behavior of a shared multi-user conversation.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
)
from tests.integration.helpers import all_message_text, failure_detail, run_turn

# EDIT level mirrored from omnigent/server/auth.py (see the sharing
# e2e suite for the full level semantics).
_LEVEL_EDIT = 2


def test_share_and_second_user_continues(
    live_server: str,
    live_runner_id: str,
    harness_name: str,
    model_name: str,
    request: pytest.FixtureRequest,
    mock_llm_server_url: str | None,
) -> None:
    suffix = uuid.uuid4().hex[:6]
    token = f"SHARED-{uuid.uuid4().hex[:8]}"

    # In mock mode: owner turn returns "ok", Bob's turn returns the token.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "ok"},
            {"text": token},
        ],
    )
    with (
        httpx.Client(base_url=live_server, timeout=300) as owner,
        httpx.Client(
            base_url=live_server,
            headers={"X-Forwarded-Email": f"bob-{suffix}@journey.test"},
            timeout=300,
        ) as bob,
    ):
        # Register AS the owner: agent lookup is permission-filtered
        # (GET /v1/sessions), so the registration session must be theirs.
        agent_name = register_inline_agent(
            owner,
            name=f"journey-share-{harness_name}-{suffix}",
            harness=harness_name,
            model=model_name,
            profile=request.config.getoption("--profile"),
            prompt="You are a terse test assistant. Follow instructions exactly.",
            mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
        )
        sid = create_runner_bound_session(owner, agent_name=agent_name, runner_id=live_runner_id)
        body_1 = run_turn(
            owner,
            session_id=sid,
            content=f"Remember this: {token}. Reply with just 'ok'.",
        )
        assert body_1["status"] == "completed", f"owner turn failed: {failure_detail(body_1)}"

        owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob.headers["X-Forwarded-Email"], "level": _LEVEL_EDIT},
        ).raise_for_status()

        body_2 = run_turn(
            bob,
            session_id=sid,
            content=(
                "Reply with the token from the first message of this "
                "conversation, exactly as given, and nothing else."
            ),
        )
        assert body_2["status"] == "completed", f"Bob's turn failed: {failure_detail(body_2)}"
        # Bob's turn sees the owner's context: the conversation, not
        # the requesting user, owns the transcript.
        assert token in all_message_text(body_2), failure_detail(body_2)

        # The owner's snapshot carries Bob's turn (cross-user
        # visibility of the shared conversation).
        snap = owner.get(f"/v1/sessions/{sid}")
        snap.raise_for_status()
        assert token in str(snap.json()["items"])
