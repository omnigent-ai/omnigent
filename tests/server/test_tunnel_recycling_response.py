"""HTTP-boundary behaviour for a planned runner recycle.

A runner recycle (server-issued ``host.stop_runner`` / managed runner-token
TTL expiry / graceful shutdown) closes the tunnel and aborts any in-flight
relayed request. This must surface to the caller as a retryable
``503 runner_recycling`` (with ``Retry-After``), not an opaque
``500 internal_error`` — otherwise a caller such as ``sys_session_create``
cannot tell a recoverable recycle from a real fault, and won't retry.

These tests exercise the two boundary halves:
- ``build_tunnel_recycling_response`` returns the right status/code/header.
- the registered exception handler routes a ``TunnelRecyclingError`` raised
  from a route to that response (proving Starlette dispatches the
  ConnectionError subclass to the specific handler, not the 500 catch-all) —
  i.e. a create that hits a recycle window now gets the retryable error.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import ErrorCode
from omnigent.runner.transports.ws_tunnel.registry import (
    RUNNER_RECYCLING_RETRY_AFTER_S,
    TunnelRecyclingError,
)
from omnigent.server.app import build_tunnel_recycling_response


def test_build_tunnel_recycling_response_shape() -> None:
    resp = build_tunnel_recycling_response(RUNNER_RECYCLING_RETRY_AFTER_S)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(RUNNER_RECYCLING_RETRY_AFTER_S)
    import json

    body = json.loads(bytes(resp.body))
    assert body["error"]["code"] == ErrorCode.RUNNER_RECYCLING


def test_build_tunnel_recycling_response_retry_after_floor() -> None:
    """Retry-After never drops below 1s even for a sub-second hint."""
    resp = build_tunnel_recycling_response(0.1)
    assert int(resp.headers["Retry-After"]) >= 1


def _app_with_handler() -> FastAPI:
    app = FastAPI()

    async def _handle(request: Request, exc: TunnelRecyclingError) -> JSONResponse:
        return build_tunnel_recycling_response(exc.retry_after_s)

    app.add_exception_handler(TunnelRecyclingError, _handle)

    @app.post("/v1/sessions")
    async def _create() -> JSONResponse:  # pragma: no cover - raises
        # Mirrors an in-flight relayed request aborted mid-recycle.
        raise TunnelRecyclingError()

    return app


def test_recycle_during_create_returns_retryable_503() -> None:
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    resp = client.post("/v1/sessions")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == ErrorCode.RUNNER_RECYCLING
    assert int(resp.headers["Retry-After"]) >= 1


def test_real_create_app_registers_recycling_handler(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """The *production* app (create_app) wires the recycling handler.

    Guards the wiring the minimal-app tests above assume: if a future
    refactor drops the ``@app.exception_handler(TunnelRecyclingError)``
    registration, the recycling error would fall through to the catch-all
    500 handler and callers would stop retrying. Builds the real app with
    the same SQLite-backed stores the integration suite uses.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    assert TunnelRecyclingError in app.exception_handlers
