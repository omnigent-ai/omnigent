"""Tests for caller-authenticated Context Saver worker proxying."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from omnigent.entities import Conversation, SessionPermission
from omnigent.errors import OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runtime.caps import RuntimeCaps
from omnigent.runtime.context_saver import (
    FocusedReadWorkerResult,
    parse_context_saver_settings,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes.sessions.routes_context_saver import (
    register_context_saver_routes,
)

_SESSION_ID = "conv_test"
_USER_ID = "owner@example.com"
_PATH = f"/v1/sessions/{_SESSION_ID}/context-saver/focused-read"
_RUNNER_TOKEN = "runner-token"


class _FixedAuthProvider(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        del request
        return _USER_ID


class _ConversationStore:
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        if conversation_id != _SESSION_ID:
            return None
        return Conversation(
            id=_SESSION_ID,
            created_at=0,
            updated_at=0,
            root_conversation_id=_SESSION_ID,
            agent_id="agent_test",
        )


class _PermissionStore:
    def __init__(self, level: int = LEVEL_EDIT) -> None:
        self.level = level

    def is_admin(self, user_id: str) -> bool:
        del user_id
        return False

    def check_access(
        self,
        user_id: str | None,
        conversation_id: str,
        required_level: int,
    ) -> bool:
        return (
            user_id == _USER_ID and conversation_id == _SESSION_ID and self.level >= required_level
        )

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        if user_id != _USER_ID or conversation_id != _SESSION_ID:
            return None
        return SessionPermission(
            user_id=user_id,
            conversation_id=conversation_id,
            level=self.level,
        )


class _Worker:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def focus(self, **kwargs: Any) -> FocusedReadWorkerResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FocusedReadWorkerResult(
            content='{"answer":"Found it.","sources":[]}',
            input_tokens=12,
            output_tokens=4,
            reported_model="databricks-glm-5-2",
        )


def _caps(worker: _Worker | None) -> RuntimeCaps:
    return RuntimeCaps(
        context_saver=parse_context_saver_settings(
            {
                "enabled": True,
                "techniques": {
                    "focused_read": {
                        "max_files": 2,
                        "max_total_bytes": 100,
                        "max_excerpt_lines": 20,
                        "request_timeout_seconds": 10,
                    }
                },
            }
        ),
        context_saver_worker=worker,
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    caps: RuntimeCaps,
    *,
    level: int = LEVEL_EDIT,
) -> FastAPI:
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_context_saver.get_caps",
        lambda: caps,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    router = APIRouter()
    register_context_saver_routes(
        router,
        conversation_store=_ConversationStore(),  # type: ignore[arg-type]
        auth_provider=_FixedAuthProvider(),
        permission_store=_PermissionStore(level),  # type: ignore[arg-type]
        runner_tunnel_tokens=frozenset({_RUNNER_TOKEN}),
    )
    app.include_router(router, prefix="/v1")
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://server",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: _RUNNER_TOKEN},
    )


def _payload(content: str = "line 1\nline 2\n") -> dict[str, Any]:
    return {
        "files": [{"path": "large.py", "content": content}],
        "question": "Where is it?",
        "model": "databricks/context-saver-cheap",
        "allow_source_upload": False,
        "timeout_seconds": 10,
        "max_excerpt_lines": 20,
        "output_budget": 400,
    }


@pytest.mark.asyncio
async def test_proxy_invokes_server_worker_with_authorized_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _Worker()
    async with _client(_app(monkeypatch, _caps(worker))) as client:
        available = await client.get(_PATH)
        response = await client.post(_PATH, json=_payload())

    assert available.json() == {"available": True}
    assert response.status_code == 200
    assert response.json()["input_tokens"] == 12
    assert response.json()["model"] == "databricks-glm-5-2"
    assert worker.calls[0]["files"][0].content == "line 1\nline 2\n"
    assert worker.calls[0]["model"] == "databricks/context-saver-cheap"


@pytest.mark.asyncio
async def test_proxy_reports_when_server_has_no_injected_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(_app(monkeypatch, _caps(None))) as client:
        available = await client.get(_PATH)
        response = await client.post(_PATH, json=_payload())

    assert available.json() == {"available": False}
    assert response.status_code == 409
    assert response.json() == {"error": "context_saver_worker_unavailable"}


@pytest.mark.asyncio
async def test_proxy_requires_edit_access_before_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _Worker()
    async with _client(_app(monkeypatch, _caps(worker), level=LEVEL_READ)) as client:
        response = await client.post(_PATH, json=_payload())

    assert response.status_code == 403
    assert worker.calls == []


@pytest.mark.asyncio
async def test_proxy_requires_bound_runner_proof_before_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _Worker()
    app = _app(monkeypatch, _caps(worker))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://server",
    ) as client:
        response = await client.post(_PATH, json=_payload())

    assert response.status_code == 403
    assert worker.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,error",
    [
        (
            {**_payload(), "model": "openai/gpt-4o-mini"},
            "worker_destination_not_approved",
        ),
        (
            _payload("source that is deliberately over one hundred bytes " * 3),
            "worker_limits_exceeded",
        ),
    ],
)
async def test_proxy_validates_destination_and_limits_before_inference(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    error: str,
) -> None:
    worker = _Worker()
    async with _client(_app(monkeypatch, _caps(worker))) as client:
        response = await client.post(_PATH, json=payload)

    assert response.status_code == 400
    assert response.json() == {"error": error}
    assert worker.calls == []


@pytest.mark.asyncio
async def test_proxy_failure_never_returns_worker_exception_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "DO_NOT_RETURN_THIS_SOURCE"
    worker = _Worker(error=RuntimeError(secret))
    async with _client(_app(monkeypatch, _caps(worker))) as client:
        response = await client.post(_PATH, json=_payload(secret))

    assert response.status_code == 502
    assert response.json() == {"error": "context_saver_worker_failed"}
    assert secret not in response.text
