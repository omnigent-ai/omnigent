"""A native sub-agent's kickoff prompt must appear once, not duplicated."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.native_coding_agents import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
)
from omnigent.server.routes import sessions as sessions_routes
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

KICKOFF = "Start the assigned task [kickoff-marker-7a1f]"
KICKOFF_MARKER = "kickoff-marker-7a1f"

NATIVE_WRAPPER_AGENTS = [
    pytest.param(agent.agent_name, agent.wrapper_label, id=agent.key)
    for agent in (CLAUDE_NATIVE_CODING_AGENT, CODEX_NATIVE_CODING_AGENT)
]


@pytest.fixture()
def bound_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubRunner:
        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json={}, request=httpx.Request("POST", "http://runner"))

    runner = _StubRunner()

    async def _resolve_bound_runner(session_id: str, runner_router: Any) -> _StubRunner:
        return runner

    async def _skip_relay_readiness(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _resolve_bound_runner)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _skip_relay_readiness)


def _kickoff_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


async def _create_subagent_with_kickoff(
    client: httpx.AsyncClient,
    *,
    parent_agent_name: str,
    child_agent_name: str,
    kickoff: str,
) -> dict[str, Any]:
    parent_agent = await create_test_agent(client, name=parent_agent_name)
    parent = await client.post("/v1/sessions", json={"agent_id": parent_agent["id"]})
    assert parent.status_code == 201, parent.text

    child_agent = await create_test_agent(client, name=child_agent_name)
    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": child_agent["id"],
            "parent_session_id": parent.json()["id"],
            "title": "impl:task-1",
            "initial_items": [_kickoff_item(kickoff)],
        },
    )
    assert child.status_code == 201, child.text
    return child.json()


async def _simulate_transcript_forwarder_echo(
    client: httpx.AsyncClient, session_id: str, text: str
) -> None:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
                "response_id": "resp_claude_echo",
            },
        },
    )
    assert resp.status_code in (200, 201, 202), resp.text


async def _kickoff_message_count(client: httpx.AsyncClient, session_id: str, marker: str) -> int:
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    return sum(
        1
        for item in items
        if item.get("type") == "message"
        and item.get("role") == "user"
        and marker in json.dumps(item.get("content", []))
    )


async def test_plain_subagent_kickoff_persisted_once(
    client: httpx.AsyncClient,
    bound_runner: None,
) -> None:
    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name="orch-plain",
        child_agent_name="impl-plain",
        kickoff=KICKOFF,
    )
    assert await _kickoff_message_count(client, child["id"], KICKOFF_MARKER) == 1


@pytest.mark.parametrize("agent_name,wrapper_value", NATIVE_WRAPPER_AGENTS)
async def test_native_subagent_kickoff_appears_once(
    client: httpx.AsyncClient,
    bound_runner: None,
    agent_name: str,
    wrapper_value: str,
) -> None:
    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name=f"orch-{agent_name}",
        child_agent_name=agent_name,
        kickoff=KICKOFF,
    )
    assert child["labels"].get(WRAPPER_LABEL_KEY) == wrapper_value

    await _simulate_transcript_forwarder_echo(client, child["id"], KICKOFF)

    assert await _kickoff_message_count(client, child["id"], KICKOFF_MARKER) == 1
