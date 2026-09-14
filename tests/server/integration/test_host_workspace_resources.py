"""Integration tests for session-free host workspace resource routes."""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import _host_workspace_resources as workspace_routes
from omnigent.server.routes._host_filesystem import HostFsError, HostFsUnavailableError
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "750367081fa442d5925648d3f9481acd"
_WORKSPACE = "/Users/alice/project"


class _FakeWebSocket:
    async def send_text(self, data: str) -> None:
        """Accept the registry protocol; route reads are mocked below."""


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: Any) -> str | None:
        return request.headers.get("X-Test-User")


@pytest.fixture()
def workspace_app(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, HostRegistry, list[dict[str, Any]]]:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    host_store.upsert_on_connect(
        host_id=_HOST_ID,
        name="alice-laptop",
        user_id="alice@example.com",
    )
    registry.register(
        host_id=_HOST_ID,
        ws=_FakeWebSocket(),  # type: ignore[arg-type]
        hello=HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name="alice-laptop",
        ),
        owner="alice@example.com",
    )

    calls: list[dict[str, Any]] = []

    async def _read(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"object": "list", "data": [], "has_more": False}

    async def _stat(**kwargs: Any) -> dict[str, Any]:
        calls.append({"stat": kwargs})
        return {
            "status": "ok",
            "exists": True,
            "type": "directory",
            "canonical_path": _WORKSPACE,
            "error": None,
        }

    monkeypatch.setattr(workspace_routes, "read_workspace_from_host", _read)
    monkeypatch.setattr(workspace_routes, "_ask_host_stat", _stat)

    app = FastAPI()
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conversation_store,
            auth_provider=_HeaderAuth(),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return app, registry, calls


async def test_environment_descriptor_uses_canonical_host_path(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/default",
            params={"workspace": "~/project"},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == ""
    assert payload["metadata"]["root"] == _WORKSPACE
    assert payload["metadata"]["reachable"] == {
        "unconfined": False,
        "roots": [{"path": _WORKSPACE, "access": "read", "origin": "cwd"}],
    }
    assert calls == [
        {
            "stat": {
                "host_registry": ANY,
                "host_conn": ANY,
                "path": "~/project",
            }
        }
    ]


async def test_filesystem_read_uses_empty_session_and_preserves_query_params(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/default/filesystem/src",
            params={"workspace": _WORKSPACE, "limit": 40, "order": "asc", "after": "src/a"},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 200
    call = calls[-1]
    assert call["workspace"] == _WORKSPACE
    assert call["session_id"] == ""
    assert call["op"] == "list_or_read"
    assert call["params"] == {
        "path": "src",
        "limit": 40,
        "after": "src/a",
        "before": None,
        "order": "asc",
    }


async def test_scoped_search_forwards_path_and_filters(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/default/search/src",
            params={
                "workspace": _WORKSPACE,
                "q": "client",
                "include": "*.ts",
                "exclude": "*.test.ts",
                "limit": 12,
            },
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 200
    assert calls[-1]["op"] == "search"
    assert calls[-1]["params"] == {
        "path": "src",
        "q": "client",
        "include": "*.ts",
        "exclude": "*.test.ts",
        "limit": 12,
    }


async def test_other_user_cannot_read_workspace(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/default/filesystem",
            params={"workspace": _WORKSPACE},
            headers={"X-Test-User": "bob@example.com"},
        )

    assert response.status_code == 403
    assert calls == []


async def test_workspace_resources_require_authentication(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources",
            params={"workspace": _WORKSPACE},
        )

    assert response.status_code == 401
    assert calls == []


async def test_relative_workspace_is_rejected_before_host_read(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/github",
            params={"workspace": "relative/project"},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 400
    assert calls == []


async def test_unknown_environment_is_rejected_after_owner_check(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/terminal_x/filesystem",
            params={"workspace": _WORKSPACE},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 404
    assert calls == []


async def test_host_filesystem_error_keeps_status(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _registry, _calls = workspace_app

    async def _fail(**kwargs: Any) -> dict[str, Any]:
        raise HostFsError(400, "invalid_path", "Path escapes the workspace root")

    monkeypatch.setattr(workspace_routes, "read_workspace_from_host", _fail)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/default/filesystem/bad",
            params={"workspace": _WORKSPACE},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Path escapes the workspace root"}


async def test_github_file_diff_forwards_revision_selection(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, _registry, calls = workspace_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/github/diff/src/app.ts",
            params={
                "workspace": _WORKSPACE,
                "pr_url": "https://github.com/acme/repo/pull/42",
                "previous_path": "src/old.ts",
                "head_sha": "abc123",
                "base_sha": "def456",
            },
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 200
    assert calls[-1]["op"] == "github_diff"
    assert calls[-1]["params"] == {
        "path": "src/app.ts",
        "pr_url": "https://github.com/acme/repo/pull/42",
        "previous_path": "src/old.ts",
        "head_sha": "abc123",
        "base_sha": "def456",
    }


async def test_missing_workspace_descriptor_returns_not_found(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _registry, _calls = workspace_app

    async def _missing(**kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "exists": False, "type": None, "canonical_path": None}

    monkeypatch.setattr(workspace_routes, "_ask_host_stat", _missing)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/environments/default",
            params={"workspace": "/missing"},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 404


async def test_offline_host_returns_conflict(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
) -> None:
    app, registry, calls = workspace_app
    registry.deregister(_HOST_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/github",
            params={"workspace": _WORKSPACE},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert calls == []


async def test_unsupported_host_timeout_is_gateway_timeout(
    workspace_app: tuple[FastAPI, HostRegistry, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _registry, _calls = workspace_app

    async def _timeout(**kwargs: Any) -> dict[str, Any]:
        raise HostFsUnavailableError("host did not respond; it may be outdated", status=504)

    monkeypatch.setattr(workspace_routes, "read_workspace_from_host", _timeout)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/workspace/resources/github",
            params={"workspace": _WORKSPACE},
            headers={"X-Test-User": "alice@example.com"},
        )

    assert response.status_code == 504
