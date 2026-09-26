"""A missing stored agent bundle produces an actionable runner-launch error."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "33296f9b15e02671c34e013dd711407e"


async def test_launch_runner_missing_agent_bundle_is_not_a_500(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """Launch fails without binding a runner when the agent bundle is missing."""
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")

    # Single-user ownership lets the request reach bundle resolution.
    app = FastAPI()
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            agent_store=agent_store,
            agent_cache=agent_cache,
        ),
        prefix="/v1",
    )

    # Map structured errors and uncaught exceptions as the production app does.
    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """Mirror the production OmnigentError handler: code -> status."""
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled_exception(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Mirror the production catch-all: unhandled -> generic 500."""
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR,
                    "message": "An internal error occurred.",
                },
            },
        )

    # Only host presence is needed before bundle resolution; no launch frame is sent.
    host_store.upsert_on_connect(_HOST_ID, "laptop", "local")
    registry.register(
        _HOST_ID,
        type(
            "FakeWS",
            (),
            {"send_text": lambda self, d: None, "receive_text": lambda self: ""},
        )(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="laptop"),
        owner="local",
    )

    # Persist the agent and session without the backing bundle.
    agent_id = "ab5e97bd41c34fa2b0c9d5c3f1e2a6d4"
    agent = agent_store.create(
        agent_id=agent_id,
        name="resume-missing-bundle",
        bundle_location=f"{agent_id}/deadbeefdeadbeef",
    )
    conv = conv_store.create_conversation(agent_id=agent.id)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": str(tmp_path)},
        )

    assert resp.status_code == 410, resp.text
    error = resp.json()["error"]
    assert error["code"] == ErrorCode.AGENT_BUNDLE_MISSING
    assert agent.name in error["message"]
    assert "Re-upload the agent" in error["message"]

    # Leave the session unbound so it can retry after the bundle is restored.
    refetched = conv_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.runner_id is None, "a failed launch must not leave a runner bound"
