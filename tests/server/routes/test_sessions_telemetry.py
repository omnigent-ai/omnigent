"""Telemetry opt-out tests for session lifecycle routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes.sessions import _session_telemetry_opted_out, create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


class _FakeTelemetryRegistry:
    """Fake host/runner registries exposing telemetry opt-out accessors."""

    def __init__(self, *, host_opted_out: bool = False, runner_opted_out: bool = False) -> None:
        self.host_opted_out = host_opted_out
        self.runner_opted_out = runner_opted_out

    def is_host_telemetry_opted_out(self, host_id: str) -> bool:
        return self.host_opted_out

    def is_runner_telemetry_opted_out(self, runner_id: str) -> bool:
        return self.runner_opted_out


class _RaisingTelemetryRegistry:
    """Registry fake that verifies telemetry gating fails open."""

    def is_host_telemetry_opted_out(self, host_id: str) -> bool:
        raise RuntimeError("host lookup failed")

    def is_runner_telemetry_opted_out(self, runner_id: str) -> bool:
        raise RuntimeError("runner lookup failed")


def _install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )


def _app(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
    *,
    host_registry: object | None = None,
    tunnel_registry: object | None = None,
) -> FastAPI:
    app = FastAPI()
    _install_error_handler(app)
    app.state.host_registry = host_registry
    app.state.tunnel_registry = tunnel_registry
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
        ),
        prefix="/v1",
    )
    return app


@pytest.fixture
def stores(db_uri: str) -> tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore]:
    return SqlAlchemyConversationStore(db_uri), SqlAlchemyAgentStore(db_uri)


def _seed_agent(agent_store: SqlAlchemyAgentStore) -> None:
    if agent_store.get("ag_test") is None:
        agent_store.create(
            agent_id="ag_test",
            name="test-agent",
            bundle_location="ag_test/bundle",
        )


def _seed_session(
    conversation_store: SqlAlchemyConversationStore,
    agent_store: SqlAlchemyAgentStore,
    *,
    host_id: str | None = None,
    runner_id: str | None = None,
) -> str:
    _seed_agent(agent_store)
    conv = conversation_store.create_conversation(
        title="telemetry session",
        agent_id="ag_test",
        host_id=host_id,
        runner_id=runner_id,
        workspace="/tmp/telemetry-session" if host_id is not None else None,
    )
    return conv.id


def test_session_telemetry_opted_out_checks_host_or_runner() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                host_registry=_FakeTelemetryRegistry(host_opted_out=True),
                tunnel_registry=_FakeTelemetryRegistry(runner_opted_out=False),
            )
        )
    )
    conv = SimpleNamespace(host_id="host_1", runner_id="runner_1")

    assert _session_telemetry_opted_out(request, conv) is True

    request.app.state.host_registry = _FakeTelemetryRegistry(host_opted_out=False)
    request.app.state.tunnel_registry = _FakeTelemetryRegistry(runner_opted_out=True)

    assert _session_telemetry_opted_out(request, conv) is True


def test_session_telemetry_opted_out_fails_open() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                host_registry=_RaisingTelemetryRegistry(),
                tunnel_registry=_RaisingTelemetryRegistry(),
            )
        )
    )
    conv = SimpleNamespace(host_id="host_1", runner_id="runner_1")

    assert _session_telemetry_opted_out(request, conv) is False


def test_create_session_skips_telemetry_when_runner_opted_out(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, agent_store = stores
    _seed_agent(agent_store)
    parent = conversation_store.create_conversation(
        title="parent",
        agent_id="ag_test",
        runner_id="runner_opted_out",
    )
    app = _app(
        conversation_store,
        agent_store,
        tunnel_registry=_FakeTelemetryRegistry(runner_opted_out=True),
    )
    emit = Mock()
    monkeypatch.setattr(sessions_routes, "_tel_emit", emit)
    monkeypatch.setattr(sessions_routes, "_get_installation_id", lambda: "install_test")

    resp = TestClient(app).post(
        "/v1/sessions",
        json={"agent_id": "ag_test", "parent_session_id": parent.id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )

    assert resp.status_code == 201
    emit.assert_not_called()


def test_create_session_emits_telemetry_without_session_opt_out(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, agent_store = stores
    _seed_agent(agent_store)
    app = _app(
        conversation_store,
        agent_store,
        host_registry=_FakeTelemetryRegistry(host_opted_out=False),
        tunnel_registry=_FakeTelemetryRegistry(runner_opted_out=False),
    )
    emit = Mock()
    monkeypatch.setattr(sessions_routes, "_tel_emit", emit)
    monkeypatch.setattr(sessions_routes, "_get_installation_id", lambda: "install_test")

    resp = TestClient(app).post(
        "/v1/sessions",
        json={"agent_id": "ag_test"},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )

    assert resp.status_code == 201
    assert emit.call_count == 1
    assert type(emit.call_args.args[0]).__name__ == "SessionCreatedEvent"


def test_stop_session_skips_telemetry_when_host_opted_out(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, agent_store = stores
    session_id = _seed_session(conversation_store, agent_store, host_id="host_opted_out")
    app = _app(
        conversation_store,
        agent_store,
        host_registry=_FakeTelemetryRegistry(host_opted_out=True),
    )
    emit = Mock()
    monkeypatch.setattr(sessions_routes, "_tel_emit", emit)
    monkeypatch.setattr(sessions_routes, "_get_installation_id", lambda: "install_test")
    monkeypatch.setattr(sessions_routes, "_stop_session_via_runner", AsyncMock(return_value=False))

    resp = TestClient(app).post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session"},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )

    assert resp.status_code == 202
    emit.assert_not_called()


def test_stop_session_emits_telemetry_without_session_opt_out(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, agent_store = stores
    session_id = _seed_session(conversation_store, agent_store)
    app = _app(conversation_store, agent_store)
    emit = Mock()
    monkeypatch.setattr(sessions_routes, "_tel_emit", emit)
    monkeypatch.setattr(sessions_routes, "_get_installation_id", lambda: "install_test")
    monkeypatch.setattr(sessions_routes, "_stop_session_via_runner", AsyncMock(return_value=False))

    resp = TestClient(app).post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session"},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )

    assert resp.status_code == 202
    assert emit.call_count == 1
    assert type(emit.call_args.args[0]).__name__ == "SessionStoppedEvent"


def test_delete_session_skips_telemetry_when_runner_opted_out(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, agent_store = stores
    session_id = _seed_session(conversation_store, agent_store, runner_id="runner_opted_out")
    app = _app(
        conversation_store,
        agent_store,
        tunnel_registry=_FakeTelemetryRegistry(runner_opted_out=True),
    )
    emit = Mock()
    monkeypatch.setattr(sessions_routes, "_tel_emit", emit)
    monkeypatch.setattr(sessions_routes, "_get_installation_id", lambda: "install_test")

    resp = TestClient(app).delete(
        f"/v1/sessions/{session_id}",
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )

    assert resp.status_code == 200
    emit.assert_not_called()


def test_delete_session_emits_telemetry_without_session_opt_out(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, agent_store = stores
    session_id = _seed_session(conversation_store, agent_store)
    app = _app(conversation_store, agent_store)
    emit = Mock()
    monkeypatch.setattr(sessions_routes, "_tel_emit", emit)
    monkeypatch.setattr(sessions_routes, "_get_installation_id", lambda: "install_test")

    resp = TestClient(app).delete(
        f"/v1/sessions/{session_id}",
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )

    assert resp.status_code == 200
    assert emit.call_count == 1
    assert type(emit.call_args.args[0]).__name__ == "SessionDeletedEvent"
