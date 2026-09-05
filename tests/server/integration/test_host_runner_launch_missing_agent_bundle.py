"""Session resume fails at runner launch with an opaque 500.

Journey (from the bug report): boot Arca -> run ``openui`` -> resume an
existing session. The CLI resume path
(:func:`omnigent.host.daemon_launch.launch_or_reuse_daemon_runner`) POSTs
``/v1/hosts/{host_id}/runners`` and the user sees::

    Error: Failed to launch a runner on host '...' (500): An internal error occurred.

Mechanism reproduced here: the launch endpoint resolves the session's bound
agent spec via ``_resolve_agent_spec_cwd`` -> ``AgentCache.load`` with NO
error guard — unlike the session-create path, which wraps the very same call
in ``except (KeyError, AttributeError, ValueError, ImportError, OSError)``.
When the agent's bundle is no longer present in the artifact store (e.g. a
server whose artifact/cache state was lost across a reboot while the DB kept
the agent and session rows), ``AgentCache.load`` raises ``KeyError``, which
escapes the endpoint and surfaces as the generic catch-all 500 the reporter
saw. A launch for a session whose agent bundle is unavailable must fail with
a structured client error, never an opaque 500.
"""

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
    """
    POST /v1/hosts/{id}/runners for a session whose agent bundle is
    missing from the artifact store must return a structured client
    error (4xx), not the generic 500 the resume CLI surfaces to users.

    Today ``_resolve_agent_spec_cwd`` calls ``AgentCache.load`` unguarded;
    the ``KeyError`` from the artifact store escapes the endpoint and the
    catch-all exception handler turns it into
    ``500 {"error": {"message": "An internal error occurred."}}`` — the
    exact failure reported for the boot-Arca -> openui -> resume flow.
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")

    # Single-user wiring (no auth_provider), mirroring the workspace-boundary
    # test: resolve_host_launch authorizes against the local owner so the
    # request reaches the agent-spec resolution directly.
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

    # Same handlers production installs (omnigent.server.app): the
    # structured OmnigentError mapper AND the catch-all, so an exception
    # escaping the endpoint surfaces exactly as the user saw it.
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

    # Host online: persisted row + live registry entry (the endpoint only
    # reads the registry before the agent-spec resolution, so a stub WS is
    # enough to get past resolve_host_launch).
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

    # An agent row whose bundle key has NO backing artifact — the state a
    # rebooted server is in when the DB survived but the artifact/cache
    # storage did not. The session (the one the user resumes) is bound to it.
    agent_id = "ab5e97bd41c34fa2b0c9d5c3f1e2a6d4"
    agent = agent_store.create(
        agent_id=agent_id,
        name="resume-missing-bundle",
        bundle_location=f"{agent_id}/deadbeefdeadbeef",
    )
    conv = conv_store.create_conversation(agent_id=agent.id)

    # The exact request the CLI resume path (launch_or_reuse_daemon_runner)
    # sends for this session.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": str(tmp_path)},
        )

    assert resp.status_code < 500, (
        f"Launching a runner for a session whose agent bundle is missing "
        f"must be a structured client error, got {resp.status_code}: "
        f"{resp.text}. The unguarded AgentCache.load in "
        f"_resolve_agent_spec_cwd/_resolve_agent_harness lets the artifact "
        f"store's KeyError escape as the generic 500 users see as "
        f"\"Failed to launch a runner on host '...' (500): An internal "
        f'error occurred." on session resume.'
    )

    # A failed launch must leave the session unbound so a later retry
    # (e.g. after re-uploading the agent) can bind cleanly.
    refetched = conv_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.runner_id is None, "a failed launch must not leave a runner bound"
