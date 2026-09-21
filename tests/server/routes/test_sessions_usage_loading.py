"""Session metadata and native launch must not wait for display usage."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.runner.native.orchestration import _codex_native_launch_config
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import create_sessions_router, routes_core
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_OWNER = "owner@example.com"
_READER = "reader@example.com"


@dataclass
class _UsageApp:
    app: FastAPI
    conversations: SqlAlchemyConversationStore
    permissions: SqlAlchemyPermissionStore
    parent_id: str
    child_id: str
    workspace: Path


def _make_app(
    conversations: SqlAlchemyConversationStore,
    agents: SqlAlchemyAgentStore,
    permissions: SqlAlchemyPermissionStore | None,
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversations,
            agent_store=agents,
            permission_store=permissions,
            auth_provider=UnifiedAuthProvider(
                source="header",
                local_single_user=permissions is None,
                header_name="X-Forwarded-Email",
                header_strip_prefix="",
            ),
        ),
        prefix="/v1",
    )
    return app


@pytest.fixture
def usage_app(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _UsageApp:
    """A real authorized session route with a priced, partially archived tree."""
    conversations = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agent = agents.create(
        agent_id="087b7cb7ac30abf4debfaa578d052ec6", name="usage-agent", bundle_location="unused"
    )
    parent = conversations.create_conversation(agent_id=agent.id, workspace=str(tmp_path))
    child = conversations.create_conversation(
        agent_id=agent.id, kind="sub_agent", parent_conversation_id=parent.id
    )
    grandchild = conversations.create_conversation(
        agent_id=agent.id, kind="sub_agent", parent_conversation_id=child.id
    )
    sibling = conversations.create_conversation(
        agent_id=agent.id, kind="sub_agent", parent_conversation_id=parent.id
    )
    for conv, model, cost in (
        (parent, "model-a", 1.0),
        (child, "model-a", 2.5),
        (grandchild, "model-b", 0.25),
        (sibling, "model-b", 4.0),
    ):
        conversations.set_session_usage(
            conv.id,
            {
                "total_cost_usd": cost,
                "by_model": {model: {"input_tokens": 10, "total_cost_usd": cost}},
            },
        )
    conversations.update_conversation(grandchild.id, archived=True)
    permissions.ensure_user(_OWNER)
    permissions.grant(_OWNER, parent.id, LEVEL_OWNER)
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: None)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://test")
    app = _make_app(conversations, agents, permissions)
    return _UsageApp(app, conversations, permissions, parent.id, child.id, tmp_path)


@pytest.fixture
async def usage_client(usage_app: _UsageApp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=usage_app.app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Forwarded-Email": _OWNER},
    ) as client:
        yield client


@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_snapshot_can_skip_usage_without_exposing_parent_only_cost(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Opting out never calls the loader or substitutes the parent's $1 spend."""
    load_usage = Mock(side_effect=AssertionError("metadata must not load usage"))
    monkeypatch.setattr(orchestration, "load_session_usage", load_usage)
    monkeypatch.setattr(
        usage_app.conversations,
        "list_conversations",
        Mock(side_effect=AssertionError("metadata must not enumerate the usage tree")),
    )
    response = await usage_client.request(
        method,
        f"/v1/sessions/{usage_app.parent_id}",
        params={"include_items": "false", "include_liveness": "false", "include_usage": "false"},
        **({"json": {"external_session_id": "thread-resumed"}} if method == "PATCH" else {}),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == usage_app.parent_id
    assert body["workspace"] == str(usage_app.workspace)
    assert body["usage_included"] is False
    assert body["total_cost_usd"] is None
    assert body["usage_by_model"] is None
    if method == "PATCH":
        assert body["external_session_id"] == "thread-resumed"
        stored = usage_app.conversations.get_conversation(usage_app.parent_id)
        assert stored is not None and stored.external_session_id == "thread-resumed"
    load_usage.assert_not_called()


async def test_real_codex_launch_config_does_not_load_usage(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the actual launch callback against the authorized server route."""
    load_usage = Mock(side_effect=AssertionError("Codex launch must not load usage"))
    monkeypatch.setattr(orchestration, "load_session_usage", load_usage)
    usage_app.conversations.update_conversation(
        usage_app.parent_id, model_override="gpt-5", reasoning_effort="high"
    )
    config = await _codex_native_launch_config(
        session_id=usage_app.parent_id, server_client=usage_client
    )
    assert config.workspace == usage_app.workspace
    assert config.model_override == "gpt-5"
    assert config.reasoning_effort == "high"
    load_usage.assert_not_called()


@pytest.mark.parametrize("include_usage", [None, "true"])
@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_default_snapshot_still_includes_complete_usage(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    include_usage: str | None,
    method: str,
) -> None:
    params = {} if include_usage is None else {"include_usage": include_usage}
    response = await usage_client.request(
        method,
        f"/v1/sessions/{usage_app.parent_id}",
        params=params,
        **({"json": {"title": "Updated title"}} if method == "PATCH" else {}),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["usage_included"] is True
    assert body["total_cost_usd"] == 7.75
    assert body["usage_by_model"]["model-a"]["total_cost_usd"] == 3.5
    assert body["usage_by_model"]["model-b"]["total_cost_usd"] == 4.25


@pytest.mark.parametrize("node, expected_cost", [("parent_id", 7.75), ("child_id", 2.75)])
async def test_usage_endpoint_sums_only_authorized_session_subtree(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    node: str,
    expected_cost: float,
) -> None:
    """A child includes archived descendants, but not its parent or siblings."""
    session_id = getattr(usage_app, node)
    response = await usage_client.get(f"/v1/sessions/{session_id}/usage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == session_id
    assert body["total_cost_usd"] == expected_cost
    assert (
        sum(model["total_cost_usd"] for model in body["usage_by_model"].values()) == expected_cost
    )
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("cost", [None, 0.0])
async def test_usage_endpoint_distinguishes_unpriced_from_zero(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    cost: float | None,
) -> None:
    solo = usage_app.conversations.create_conversation()
    usage_app.permissions.grant(_OWNER, solo.id, LEVEL_OWNER)
    usage_app.conversations.set_session_usage(
        solo.id, {} if cost is None else {"total_cost_usd": cost}
    )
    response = await usage_client.get(f"/v1/sessions/{solo.id}/usage")
    assert response.status_code == 200, response.text
    assert response.json() == {"id": solo.id, "total_cost_usd": cost, "usage_by_model": None}


@pytest.mark.parametrize("suffix", ["?include_usage=false", "/usage"])
async def test_usage_reads_require_session_access(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    load_usage = Mock(side_effect=AssertionError("must authorize before loading usage"))
    monkeypatch.setattr(routes_core, "load_session_usage", load_usage)
    monkeypatch.setattr(orchestration, "load_session_usage", load_usage)
    response = await usage_client.get(
        f"/v1/sessions/{usage_app.parent_id}{suffix}",
        headers={"X-Forwarded-Email": _READER},
    )
    assert response.status_code == 404, response.text
    load_usage.assert_not_called()


async def test_usage_endpoint_accepts_read_only_collaborator(
    usage_app: _UsageApp, usage_client: httpx.AsyncClient
) -> None:
    usage_app.permissions.ensure_user(_READER)
    usage_app.permissions.grant(_READER, usage_app.parent_id, LEVEL_READ)
    response = await usage_client.get(
        f"/v1/sessions/{usage_app.parent_id}/usage", headers={"X-Forwarded-Email": _READER}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total_cost_usd"] == 7.75


async def test_usage_opt_out_does_not_authorize_read_only_metadata_writes(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_app.permissions.ensure_user(_READER)
    usage_app.permissions.grant(_READER, usage_app.parent_id, LEVEL_READ)
    load_usage = Mock(side_effect=AssertionError("must authorize before loading usage"))
    monkeypatch.setattr(orchestration, "load_session_usage", load_usage)
    response = await usage_client.patch(
        f"/v1/sessions/{usage_app.parent_id}",
        params={"include_usage": "false"},
        json={"external_session_id": "unauthorized-thread"},
        headers={"X-Forwarded-Email": _READER},
    )
    assert response.status_code == 403, response.text
    stored = usage_app.conversations.get_conversation(usage_app.parent_id)
    assert stored is not None and stored.external_session_id is None
    load_usage.assert_not_called()


@pytest.mark.parametrize("mode", ["admin", "single-user"])
async def test_usage_endpoint_reads_row_when_authorization_does_not(
    usage_app: _UsageApp,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Admin and single-user access still look up the row and reject missing ids."""
    if mode == "admin":
        usage_app.permissions.set_admin(_OWNER, True)
        app = usage_app.app
    else:
        app = _make_app(usage_app.conversations, SqlAlchemyAgentStore(db_uri), None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": _OWNER},
    ) as client:
        response = await client.get(f"/v1/sessions/{usage_app.parent_id}/usage")
        assert response.status_code == 200, response.text
        assert response.json()["total_cost_usd"] == 7.75

        load_usage = Mock(side_effect=AssertionError("missing sessions must not load usage"))
        monkeypatch.setattr(routes_core, "load_session_usage", load_usage)
        missing = await client.get("/v1/sessions/00000000000000000000000000000000/usage")
        assert missing.status_code == 404, missing.text
        load_usage.assert_not_called()


async def test_usage_endpoint_requires_authentication(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_usage = Mock(side_effect=AssertionError("must authenticate before loading usage"))
    monkeypatch.setattr(routes_core, "load_session_usage", load_usage)
    response = await usage_client.get(
        f"/v1/sessions/{usage_app.parent_id}/usage", headers={"X-Forwarded-Email": ""}
    )
    assert response.status_code == 401, response.text
    load_usage.assert_not_called()


async def test_usage_endpoint_preserves_failures_instead_of_fabricating_zero(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routes_core, "load_session_usage", Mock(side_effect=RuntimeError("tree unavailable"))
    )
    failed = await usage_client.get(f"/v1/sessions/{usage_app.parent_id}/usage")
    assert failed.status_code == 500
    snapshot = await usage_client.get(
        f"/v1/sessions/{usage_app.parent_id}", params={"include_usage": "false"}
    )
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["total_cost_usd"] is None
    assert snapshot.json()["usage_included"] is False


async def test_slow_usage_request_does_not_hold_session_metadata(
    usage_app: _UsageApp,
    usage_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A separate, blocked tree read leaves the metadata response available."""
    usage_started = asyncio.Event()
    release_usage = threading.Event()
    loop = asyncio.get_running_loop()
    original_loader = routes_core.load_session_usage

    def delayed_usage(*args: Any, **kwargs: Any) -> dict[str, Any]:
        loop.call_soon_threadsafe(usage_started.set)
        assert release_usage.wait(timeout=10), "test did not release the usage read"
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(routes_core, "load_session_usage", delayed_usage)
    usage_task = asyncio.create_task(usage_client.get(f"/v1/sessions/{usage_app.parent_id}/usage"))
    try:
        await asyncio.wait_for(usage_started.wait(), timeout=3)
        snapshot = await asyncio.wait_for(
            usage_client.get(
                f"/v1/sessions/{usage_app.parent_id}", params={"include_usage": "false"}
            ),
            timeout=3,
        )
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["usage_included"] is False
        assert not usage_task.done()
    finally:
        release_usage.set()
        usage_response = await usage_task
    assert usage_response.status_code == 200, usage_response.text
    assert usage_response.json()["total_cost_usd"] == 7.75
