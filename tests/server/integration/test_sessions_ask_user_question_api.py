"""
Integration tests for ``POST /v1/sessions/{id}/ask_user_question``.

Exercises the actual FastAPI route (not just the pure
``_ask_user_question`` helpers unit-tested in
``tests/server/routes/test_ask_user_question.py``): the runner-invoked
POST publishes a ``response.elicitation_request`` SSE event through the
SAME elicitation engine the Claude-native ``PermissionRequest`` ASK gate
uses, and blocks until the human answers via the existing
``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` endpoint —
proving the round trip is wired end to end, not just unit-correct.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_elicitation_resolve_url import (
    _drain_until_elicitation,
)

pytestmark = pytest.mark.asyncio

_QUESTIONS = [
    {
        "question": "Which framework?",
        "header": "Framework",
        "options": [
            {"label": "React", "description": "Component-based UI library."},
            {"label": "Vue", "description": "Progressive framework.", "recommended": True},
        ],
        "multiSelect": False,
    }
]


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """Create a minimal session and return its id."""
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def test_accept_round_trip_reconstructs_answers(client: httpx.AsyncClient) -> None:
    """
    A human accept with a flat answers map comes back as the tool's
    rich, reconstructed ``{questions, answers}`` output — proving the
    route wires ``validate`` -> ``build_ask_user_question_params`` ->
    the shared elicitation engine -> ``reconstruct`` end to end.
    """
    agent = await create_test_agent(client, "test-ask-user-question-accept")
    session_id = await _create_session(client, agent["id"])

    subscribed = asyncio.Event()
    drain_task = asyncio.ensure_future(_drain_until_elicitation(session_id, subscribed=subscribed))
    await subscribed.wait()

    ask_task = asyncio.ensure_future(
        client.post(
            f"/v1/sessions/{session_id}/ask_user_question",
            json={"questions": _QUESTIONS},
        )
    )
    elicitation_id = await drain_task

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept", "content": {"Which framework?": "Vue"}},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await ask_task
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["answers"] == {"Which framework?": "Vue"}
    assert body["questions"][0]["options"][1]["recommended"] is True
    assert "error" not in body


async def test_decline_round_trip_returns_error_shape(client: httpx.AsyncClient) -> None:
    """A human decline comes back as a clean error, not a raised exception."""
    agent = await create_test_agent(client, "test-ask-user-question-decline")
    session_id = await _create_session(client, agent["id"])

    subscribed = asyncio.Event()
    drain_task = asyncio.ensure_future(_drain_until_elicitation(session_id, subscribed=subscribed))
    await subscribed.wait()

    ask_task = asyncio.ensure_future(
        client.post(
            f"/v1/sessions/{session_id}/ask_user_question",
            json={"questions": _QUESTIONS},
        )
    )
    elicitation_id = await drain_task

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "decline"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await ask_task
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answers"] == {}
    assert "declined" in body["error"]


async def test_invalid_questions_returns_400(client: httpx.AsyncClient) -> None:
    """A malformed ``questions`` argument is rejected before any elicitation
    is published — 400, not a hang."""
    agent = await create_test_agent(client, "test-ask-user-question-invalid")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/ask_user_question",
        json={"questions": []},
    )
    assert resp.status_code == 400, resp.text
