"""Tests for isolated framework checkpoint session routes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes.sessions import routes_checkpoint
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest_asyncio.fixture()
async def checkpoint_session_id(db_uri: str) -> str:
    """Seed a session with unrelated policy state."""
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agents.create(agent_id, name="checkpoint-agent", bundle_location="test:///checkpoint")
    conversation = conversations.create_conversation(agent_id=agent_id)
    conversations.set_session_state(conversation.id, {"policy.keep": {"mode": "enforce"}})
    return conversation.id


def _checkpoint(session_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "session_id": session_id,
        "status": "idle",
        "latest_user_directive": "Just create a PR",
        "phase": "open_pr",
        "verified_actions": [
            {
                "name": "shell",
                "call_id": "push",
                "outcome": "success",
                "markers": ["git_push", "exit_code:0"],
            }
        ],
        "failed_actions": [],
        "do_not_repeat": ["Do not repeat the verified git push."],
        "pending": "Create the pull request with github__create_pull_request.",
        "covered_items": ["d" * 64],
        "history_fingerprint": "e" * 64,
        "updated_at": "2026-08-08T20:00:00+00:00",
    }


async def test_checkpoint_api_isolates_framework_state(
    client: httpx.AsyncClient,
    checkpoint_session_id: str,
    db_uri: str,
) -> None:
    """Replacing a checkpoint preserves unrelated policy session state."""
    response = await client.put(
        f"/v1/sessions/{checkpoint_session_id}/checkpoint",
        json={"checkpoint": _checkpoint(checkpoint_session_id)},
    )

    assert response.status_code == 200
    assert response.json()["checkpoint"]["pending"].startswith("Create the pull request")

    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(checkpoint_session_id)
    assert conversation is not None
    assert conversation.session_state["policy.keep"] == {"mode": "enforce"}
    assert (
        conversation.session_state["_framework_checkpoint_v1"]["session_id"]
        == checkpoint_session_id
    )

    response = await client.get(f"/v1/sessions/{checkpoint_session_id}/checkpoint")
    assert response.status_code == 200
    assert response.json()["session_id"] == checkpoint_session_id
    assert response.json()["checkpoint"]["session_id"] == checkpoint_session_id
    assert response.json()["checkpoint"]["phase"] == "open_pr"


async def test_checkpoint_api_rejects_cross_session_payload(
    client: httpx.AsyncClient,
    checkpoint_session_id: str,
) -> None:
    """A checkpoint cannot be written through another session's route."""
    response = await client.put(
        f"/v1/sessions/{checkpoint_session_id}/checkpoint",
        json={"checkpoint": _checkpoint("conv_other")},
    )

    assert response.status_code == 400


async def test_checkpoint_write_replaces_malformed_legacy_state(
    client: httpx.AsyncClient,
    checkpoint_session_id: str,
    db_uri: str,
) -> None:
    """A valid replacement repairs an older checkpoint shape."""
    conversations = SqlAlchemyConversationStore(db_uri)
    conversations.set_session_state(
        checkpoint_session_id,
        {
            "policy.keep": {"mode": "enforce"},
            "_framework_checkpoint_v1": {
                "version": 0,
                "arguments": "legacy raw state",
            },
        },
    )

    response = await client.put(
        f"/v1/sessions/{checkpoint_session_id}/checkpoint",
        json={"checkpoint": _checkpoint(checkpoint_session_id)},
    )

    assert response.status_code == 200
    conversation = conversations.get_conversation(checkpoint_session_id)
    assert conversation is not None
    assert conversation.session_state["policy.keep"] == {"mode": "enforce"}
    assert conversation.session_state["_framework_checkpoint_v1"]["version"] == 1
    assert "arguments" not in conversation.session_state["_framework_checkpoint_v1"]


async def test_checkpoint_write_preserves_concurrent_unrelated_session_state(
    client: httpx.AsyncClient,
    checkpoint_session_id: str,
    db_uri: str,
) -> None:
    """The key-scoped checkpoint write does not replace a policy-state update."""
    conversations = SqlAlchemyConversationStore(db_uri)

    response, _ = await asyncio.gather(
        client.put(
            f"/v1/sessions/{checkpoint_session_id}/checkpoint",
            json={"checkpoint": _checkpoint(checkpoint_session_id)},
        ),
        asyncio.to_thread(
            conversations.set_session_state_key,
            checkpoint_session_id,
            "policy.concurrent",
            {"mode": "audit"},
        ),
    )

    assert response.status_code == 200
    conversation = conversations.get_conversation(checkpoint_session_id)
    assert conversation is not None
    assert conversation.session_state["policy.keep"] == {"mode": "enforce"}
    assert conversation.session_state["policy.concurrent"] == {"mode": "audit"}
    assert "_framework_checkpoint_v1" in conversation.session_state


async def test_checkpoint_route_records_tool_observation_attributes(
    client: httpx.AsyncClient,
    checkpoint_session_id: str,
    monkeypatch: Any,
) -> None:
    """Checkpoint writes redact captured tool-observation payloads."""
    attributes: dict[str, Any] = {}

    class _Span:
        def set_attribute(self, key: str, value: Any) -> None:
            attributes[key] = value

        def is_recording(self) -> bool:
            return True

    @contextmanager
    def _span(*args: Any, **kwargs: Any) -> Iterator[_Span]:
        attributes.update(kwargs["attributes"])
        yield _Span()

    monkeypatch.setattr(routes_checkpoint.telemetry, "span", _span)
    monkeypatch.setattr(routes_checkpoint.telemetry, "should_capture_content", lambda: True)

    payload = _checkpoint(checkpoint_session_id)
    payload["latest_user_directive"] = (
        "Use https://trace-token@github.com/example/repository"
    )
    response = await client.put(
        f"/v1/sessions/{checkpoint_session_id}/checkpoint",
        json={"checkpoint": payload},
    )

    assert response.status_code == 200
    assert attributes["openinference.span.kind"] == "TOOL"
    assert attributes["openinference.tool.name"] == "session_checkpoint"
    assert attributes["session.id"] == checkpoint_session_id
    assert attributes["checkpoint.outcome"] == "success"
    assert attributes["checkpoint.covered_item_count"] == 1
    assert attributes["checkpoint.latency_ms"] >= 0
    assert "trace-token" not in attributes["input.value"]
    assert "[redacted]" in attributes["input.value"]
