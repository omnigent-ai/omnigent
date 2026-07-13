"""Route regression tests for INPUT policy DENY persistence."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import ANY, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.policies.types import PolicyAction, PolicyResult
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.spec import AgentSpec
from omnigent.spec.types import GuardrailsSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_CACHE_PATCH = "omnigent.server.routes.sessions.get_agent_cache"
_ENGINE_PATCH = "omnigent.server.routes.sessions.build_policy_engine"
_STREAM_PATCH = "omnigent.server.routes.sessions.session_stream"


@pytest.fixture
def route_client(db_uri: str) -> Iterator[tuple[TestClient, str]]:
    """Build a sessions route client with one agent-bound session."""
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(
        agent_id="ag_test",
        name="test-agent",
        bundle_location="ag_test/bundle",
    )
    conv = conversation_store.create_conversation(
        title="policy session",
        agent_id="ag_test",
    )

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
        ),
        prefix="/v1",
    )

    with TestClient(app) as client:
        yield client, conv.id


def test_input_policy_deny_persists_item_readable_from_items_api(
    route_client: tuple[TestClient, str],
) -> None:
    """Synchronous INPUT DENY both streams and persists the deny sentinel."""
    client, session_id = route_client
    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=[]),
    )
    deny_result = PolicyResult(
        action=PolicyAction.DENY,
        reason="Request contains BLOCK_THIS_TOKEN",
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return deny_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
        patch(_STREAM_PATCH) as mock_stream,
    ):
        mock_cache.return_value.load.return_value.spec = spec
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Trigger BLOCK_THIS_TOKEN please.",
                        }
                    ],
                },
            },
        )

    assert resp.status_code == 202
    assert resp.json() == {
        "queued": False,
        "denied": True,
        "reason": "Request contains BLOCK_THIS_TOKEN",
    }
    mock_stream.publish.assert_any_call(
        session_id,
        {
            "type": "response.output_text.delta",
            "delta": "[Denied by policy: Request contains BLOCK_THIS_TOKEN]",
            "message_id": ANY,
            "index": 0,
        },
    )

    items_resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
    assert items_resp.status_code == 200
    items = items_resp.json()["data"]
    assert [item["type"] for item in items] == ["message"]
    persisted = items[0]
    assert persisted["role"] == "assistant"
    assert persisted["model"] == "test-agent"
    assert persisted["content"] == [
        {
            "type": "output_text",
            "text": "[Denied by policy: Request contains BLOCK_THIS_TOKEN]",
        }
    ]
