"""``POST /events`` with ``type=function_call_output`` is translated into a
``tool_result`` event and forwarded to the bound runner.

This is the AP-side half of client-side tool tunneling: a client tool's
result arrives on the session events endpoint as ``function_call_output``,
but the harness scaffold resolves a parked tool only on a ``tool_result``
event. The route must translate the wire shape and forward it. The E2E
suite covers this end-to-end, but a regression here surfaces only as an
opaque turn timeout — these focused tests pin the translation and the
fail-loud behavior with a stubbed runner (no real harness needed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

_ALICE = "alice@example.com"


def _seed_session(db_uri: str) -> str:
    """Create a conversation and grant Alice edit access."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(_ALICE)
    perm_store.grant(_ALICE, conv.id, LEVEL_EDIT)
    return conv.id


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with header-mode auth + permission_store so access is gated."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app (no real runner)."""
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _CaptureRunnerClient:
    """Stub runner client that records the forwarded POST and returns 202."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        self._calls.append({"path": path, "json": json})

        class _Resp:
            status_code = 202

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_function_call_output_forwarded_as_tool_result(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route translates function_call_output → tool_result verbatim.

    Pins the exact forwarded body. If the translation regressed (wrong
    type, dropped/renamed field), the harness scaffold would never resolve
    the parked tool and the turn would hang — this asserts the shape the
    scaffold's ToolResultEvent requires.
    """
    from omnigent.server.routes import sessions as sessions_mod

    calls: list[dict[str, Any]] = []

    async def _stub(*_: Any, **__: Any) -> _CaptureRunnerClient:
        return _CaptureRunnerClient(calls)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    session_id = _seed_session(db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "function_call_output",
            "data": {"call_id": "call_x", "output": "calc-result"},
        },
        headers={"X-Forwarded-Email": _ALICE},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": True, "item_id": "call_x"}
    # Exactly one forward, carrying the tool_result wire shape the scaffold
    # resolves a parked tool on — NOT the raw function_call_output.
    assert len(calls) == 1
    assert calls[0]["path"] == f"/v1/sessions/{session_id}/events"
    assert calls[0]["json"] == {
        "type": "tool_result",
        "call_id": "call_x",
        "output": "calc-result",
    }


@pytest.mark.asyncio
async def test_function_call_output_no_runner_returns_503(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bound runner → 503 (the result can't be delivered)."""
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub_none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub_none)
    session_id = _seed_session(db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "function_call_output", "data": {"call_id": "c", "output": "o"}},
        headers={"X-Forwarded-Email": _ALICE},
    )

    assert resp.status_code == 503, resp.text


@pytest.mark.asyncio
async def test_function_call_output_runner_error_returns_503(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure forwarding the tool_result fails loud (503).

    Best-effort would let the caller think delivery succeeded while the
    parked turn hangs to timeout; the route instead surfaces the failure
    so the caller can retry.
    """
    from omnigent.server.routes import sessions as sessions_mod

    class _FailingRunnerClient:
        async def post(self, *_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("runner unreachable")

        async def get(self, *_: Any, **__: Any) -> Any:
            raise NotImplementedError

    async def _stub_failing(*_: Any, **__: Any) -> _FailingRunnerClient:
        return _FailingRunnerClient()

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub_failing)
    session_id = _seed_session(db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "function_call_output", "data": {"call_id": "c", "output": "o"}},
        headers={"X-Forwarded-Email": _ALICE},
    )

    assert resp.status_code == 503, resp.text
