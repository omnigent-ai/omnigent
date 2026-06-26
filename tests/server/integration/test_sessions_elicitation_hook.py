"""
Integration tests for ``POST /v1/sessions/{id}/hooks/elicitation``.

The endpoint receives Claude Code's ``Elicitation`` HTTP hook payload —
fired when a third-party MCP server requests input mid-tool-call (the
MCP ``elicitation/create`` flow) and ``omnigent claude`` wraps the native
TUI. It parks on the same in-memory elicitation registry the
permission-request path uses, emits a ``response.elicitation_request``
SSE event (marked as the generic MCP form so the web UI renders
``requestedSchema`` as a typed form), and returns Claude's
``hookSpecificOutput`` ``action``/``content`` JSON once the UI verdict
arrives.

Tests cover three paths:

- Accept: the UI submits the form → endpoint returns
  ``action == "accept"`` with the submitted ``content``.
- Decline: the UI declines → ``action == "decline"`` (no content).
- URL mode: the elicitation has no inline form → endpoint returns
  ``200`` with empty body so Claude handles the out-of-band flow in
  its TUI (fail-ask).

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
so the tests exercise the real route → ``_harness_elicitation_registry``
→ SSE-publish pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runtime import session_stream
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a minimal session and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _drain_until_elicitation(
    session_id: str,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` event arrives, then return it.

    :param session_id: Session to subscribe to.
    :param timeout_s: Maximum seconds to wait before failing the test.
    :returns: The captured elicitation_request event dict.
    """
    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                elicitation_id = event.get("elicitation_id")
                assert isinstance(elicitation_id, str) and elicitation_id, (
                    f"elicitation event missing id: {event!r}"
                )
                return event
    raise AssertionError("subscribe loop ended without an elicitation event")


async def _post_approval(
    client: httpx.AsyncClient,
    session_id: str,
    elicitation_id: str,
    action: str,
    content: dict[str, Any] | None = None,
) -> httpx.Response:
    """
    Resolve a published elicitation through the session event API.

    :param client: Test HTTP client.
    :param session_id: Session that emitted the elicitation.
    :param elicitation_id: Elicitation id from the stream event.
    :param action: MCP ``ElicitResult.action`` literal.
    :param content: Optional MCP ``ElicitResult.content`` payload.
    :returns: The HTTP response from the session event route.
    """
    data: dict[str, Any] = {"elicitation_id": elicitation_id, "action": action}
    if content is not None:
        data["content"] = content
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "approval", "data": data},
    )


def _claude_elicitation_payload(*, mode: str = "form") -> dict[str, Any]:
    """
    Build a realistic Claude ``Elicitation`` hook body.

    Mirrors the empirically-captured wire shape: snake_case
    ``requested_schema`` and ``mcp_server_name`` alongside the common
    hook fields.

    :param mode: Elicitation mode, ``"form"`` or ``"url"``.
    :returns: JSON-serializable hook payload.
    """
    return {
        "session_id": "claude_sess_abc",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
        "permission_mode": "default",
        "hook_event_name": "Elicitation",
        "mcp_server_name": "elicit-demo",
        "message": "Approve the following?",
        "mode": mode,
        "requested_schema": {
            "type": "object",
            "properties": {
                "approve": {
                    "type": "boolean",
                    "title": "Approve",
                    "description": "Approve this action?",
                },
                "note": {"type": "string", "title": "Note", "default": ""},
            },
            "required": ["approve"],
        },
    }


async def test_elicitation_hook_accept_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    UI submits the MCP form → endpoint returns ``action == "accept"``
    with the submitted content in Claude's Elicitation hook shape.

    Also asserts the published elicitation marks itself as the generic
    MCP form (``policy_name``) and carries ``requestedSchema`` through
    verbatim — the two things the web form renderer keys on. Catches: SSE
    never emitted, schema not forwarded (UI can't build the form), or the
    verdict→hookSpecificOutput mapping dropping the content.
    """
    agent = await create_test_agent(client, "test-elicitation-accept")
    session_id = await _create_session(client, agent["id"])
    payload = _claude_elicitation_payload()

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    # Let the subscriber register before the publisher fires (publish is
    # broadcast-to-current-subscribers; pre-subscribe events are lost).
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/hooks/elicitation", json=payload)
    )

    event = await drain_task
    params = event["params"]
    assert params["mode"] == "form"
    assert params["policy_name"] == "claude_native_mcp_elicitation"
    assert params["requestedSchema"] == payload["requested_schema"]
    assert params.get("mcp_server_name") == "elicit-demo"

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"approve": True, "note": "ship it"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "hookSpecificOutput": {
            "hookEventName": "Elicitation",
            "action": "accept",
            "content": {"approve": True, "note": "ship it"},
        }
    }


async def test_elicitation_hook_decline_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    UI declines → endpoint returns ``action == "decline"`` with no
    content, so the MCP server sees an explicit refusal.
    """
    agent = await create_test_agent(client, "test-elicitation-decline")
    session_id = await _create_session(client, agent["id"])
    payload = _claude_elicitation_payload()

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/hooks/elicitation", json=payload)
    )

    event = await drain_task
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "decline")
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "hookSpecificOutput": {"hookEventName": "Elicitation", "action": "decline"}
    }


async def test_elicitation_hook_url_mode_fail_asks(
    client: httpx.AsyncClient,
) -> None:
    """
    A ``url``-mode elicitation has no inline form to render, so the
    endpoint returns ``200`` with an empty body immediately (no SSE, no
    parking) and Claude handles the out-of-band flow in its TUI.
    """
    agent = await create_test_agent(client, "test-elicitation-url")
    session_id = await _create_session(client, agent["id"])
    payload = _claude_elicitation_payload(mode="url")

    resp = await client.post(f"/v1/sessions/{session_id}/hooks/elicitation", json=payload)
    assert resp.status_code == 200
    assert resp.text == ""
