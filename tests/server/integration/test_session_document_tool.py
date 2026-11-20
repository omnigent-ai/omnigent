"""Exercise the document setter against the real session PATCH and SQL store."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.runner.identity import (
    MCP_SESSION_ID_HEADER,
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes.sessions import routes_agent
from omnigent.session_documents import (
    GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX,
    GOOGLE_DOCS_SELECTION_LABEL,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_scheduled_tasks_routes import _build_app, _headers, _make_user

_OWNER = "owner@example.test"
_EDITOR = "editor@example.test"
_VIEWER = "viewer@example.test"
_TOKEN = "docs-test-runner"
_DOCS = "https://docs.google.com/document"
_URL = f"{_DOCS}/d/next/edit?tab=t.notes"
_SELECTION = GOOGLE_DOCS_SELECTION_LABEL
_PREFIX = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX
_PATCH = {_SELECTION: _URL, f"{_PREFIX}next": _URL}
_LEGACY = f"{_DOCS}/d/legacy/edit?tab=t.0"
_RETAINED = f"{_DOCS}/d/retained/edit"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(db_uri, tmp_path)
    for user in (_OWNER, _EDITOR, _VIEWER):
        _make_user(db_uri, user)
    return app


@pytest.fixture()
def sessions(db_uri: str) -> tuple[str, str]:
    agent = SqlAlchemyAgentStore(db_uri).create(
        agent_id="a" * 32,
        name="document-test",
        bundle_location="document-test/bundle",
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    shared = conversations.create_conversation(agent_id=agent.id, title="Shared")
    other = conversations.create_conversation(agent_id=agent.id, title="Other")
    for session in (shared, other):
        permissions.grant(_OWNER, session.id, LEVEL_OWNER)
        conversations.set_runner_id(session.id, token_bound_runner_id(_TOKEN))
    permissions.grant(_EDITOR, shared.id, LEVEL_EDIT)
    permissions.grant(_VIEWER, shared.id, LEVEL_READ)
    conversations.set_labels(
        shared.id, {_SELECTION: _LEGACY, f"{_PREFIX}retained": _RETAINED, "unrelated": "preserved"}
    )
    return shared.id, other.id


def _headers_for_runner(session: str, user: str) -> dict[str, str]:
    return {
        **_headers(user),
        ORCHESTRATION_SOURCE_HEADER: session,
        RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN,
    }


async def test_bound_editor_can_attach_and_normalize_documents_in_its_shared_session(
    app: FastAPI, sessions: tuple[str, str], db_uri: str
) -> None:
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "google_docs"})
    shared, other = sessions
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://server.example",
        headers={**_headers(_EDITOR), RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN},
    ) as client:
        for supplied, saved in (
            (f"{_DOCS}/d/legacy/edit#tab=t.0", _LEGACY),
            (f"{_DOCS}/u/1/d/next/edit?usp=sharing#tab=t.notes", _URL),
        ):
            result = await execute_tool(
                tool_name="sys_session_set_document",
                arguments=json.dumps({"url": supplied}),
                conversation_id=shared,
                server_client=client,
            )
            assert json.loads(result) == {"url": saved}
        response = await client.get(f"/v1/sessions/{shared}/labels")
        assert response.status_code == 200, response.text
        assert response.json()["labels"] == {
            **_PATCH,
            f"{_PREFIX}legacy": "",
            f"{_PREFIX}retained": _RETAINED,
            "unrelated": "preserved",
        }
    assert SqlAlchemyConversationStore(db_uri).get_conversation(other).labels == {}


async def test_document_patch_does_not_extend_other_shared_session_permissions(
    app: FastAPI, sessions: tuple[str, str], db_uri: str
) -> None:
    shared, other = sessions
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "google_docs"})
    path = f"/v1/sessions/{shared}"
    runner_headers = _headers_for_runner(shared, _OWNER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server.example"
    ) as client:
        for target, headers, body, status in (
            (path, _headers_for_runner(shared, _VIEWER), {"labels": _PATCH}, 403),
            (path, _headers_for_runner(other, _OWNER), {"labels": _PATCH}, 403),
            (f"/v1/sessions/{other}", runner_headers, {"labels": _PATCH}, 403),
            (
                path,
                {**runner_headers, RUNNER_TUNNEL_TOKEN_HEADER: "wrong"},
                {"labels": _PATCH},
                403,
            ),
            (
                path,
                {**_headers(_OWNER), RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN},
                {"labels": _PATCH},
                403,
            ),
            (
                path,
                {**_headers(_OWNER), ORCHESTRATION_SOURCE_HEADER: shared},
                {"labels": _PATCH},
                403,
            ),
            (path, runner_headers, {"labels": _PATCH, "title": "forged"}, 403),
            (path, runner_headers, {"labels": {"unrelated": "forged"}}, 403),
            (path, runner_headers, {"labels": {**_PATCH, "unrelated": "forged"}}, 409),
            (path, runner_headers, {"labels": {**_PATCH, f"{_PREFIX}retained": ""}}, 409),
            (path, runner_headers, {"labels": {**_PATCH, _SELECTION: f"{_URL}#heading=h.x"}}, 400),
        ):
            refused = await client.patch(target, headers=headers, json=body)
            assert refused.status_code == status, refused.text
    conversations = SqlAlchemyConversationStore(db_uri)
    assert conversations.get_conversation(shared).title == "Shared"
    assert conversations.get_conversation(shared).labels == {
        _SELECTION: _LEGACY,
        f"{_PREFIX}retained": _RETAINED,
        "unrelated": "preserved",
    }
    assert conversations.get_conversation(other).labels == {}


async def test_disabled_server_refuses_agent_document_patches(
    app: FastAPI, sessions: tuple[str, str]
) -> None:
    shared, _ = sessions
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server.example"
    ) as client:
        response = await client.get("/v1/info")
        assert response.json()["features"]["google_docs"] is False
        assert response.json()["session_set_document"] is False
        write = await client.patch(
            f"/v1/sessions/{shared}",
            json={"labels": _PATCH},
            headers=_headers_for_runner(shared, _OWNER),
        )
        assert write.status_code == 403, write.text


async def test_mcp_gateway_rejects_other_sources_before_forwarding_document_tool(
    app: FastAPI, sessions: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared, other = sessions
    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "google_docs"})
    forwarded = []

    async def forward(rpc_id, session_id, params, *_args, **_kwargs):
        # Stub the downstream runner tunnel so the API authorization boundary is isolated.
        forwarded.append((session_id, params))
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {}})

    monkeypatch.setattr(routes_agent, "_handle_mcp_tools_call", forward)
    params = {"name": "sys_session_set_document", "arguments": {"url": _URL}}
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    source = {MCP_SESSION_ID_HEADER: shared}
    token = {RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server.example"
    ) as client:
        for target, headers in (
            (shared, {**_headers(_VIEWER), **source, **token}),
            (shared, {**_headers(_OWNER), **token}),
            (shared, {**_headers(_OWNER), **source}),
            (shared, {**_headers(_OWNER), **source, RUNNER_TUNNEL_TOKEN_HEADER: "wrong"}),
            (other, {**_headers(_OWNER), **source, **token}),
            (shared, {**_headers(_OWNER), MCP_SESSION_ID_HEADER: other, **token}),
        ):
            denied = await client.post(f"/v1/sessions/{target}/mcp", headers=headers, json=rpc)
            assert denied.status_code == 403, denied.text
        allowed = await client.post(
            f"/v1/sessions/{shared}/mcp",
            headers={**_headers(_EDITOR), **source, **token},
            json=rpc,
        )
        assert allowed.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert forwarded == [(shared, params)]
