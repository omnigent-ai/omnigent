"""Shared setup for sessions that continue in a new session (``/clear``, ``/rewind``).

A live runner whose HTTP side is a mock transport, a placed two-turn session,
and a managed sandbox host. Tests keep their own faults and assertions.
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update

from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher, create_test_agent, register_test_runner

_RUNNER_ID = "runner_live"
_REVIEW_SKILL = {"name": "review", "description": "Review the change.", "content": "Review it."}
_OWNER = "alice@example.com"
_EDITOR = "bob@example.com"
_READER = "carol@example.com"
_STRANGER = "dave@example.com"
_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


@dataclass
class LiveRunner:
    """What the fake live runner received, and how it answers ``clear``."""

    # ``None`` times the ``clear`` out at the transport.
    clear_status: int | None = 200
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    superseded: list[tuple[str, str, str]] = field(default_factory=list)
    # Sessions announced to their owner's open tabs.
    announced: list[str] = field(default_factory=list)
    # Runs while the runner handles ``clear``; awaited when it returns a coroutine.
    on_clear: Callable[[], object] | None = None


@asynccontextmanager
async def install_live_runner(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveRunner]:
    """Register a live runner on *app* and record what the server sends it."""
    state = LiveRunner()

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        state.posts.append((request.url.path, body))
        if body.get("type") == "clear":
            if state.on_clear is not None:
                during_release = state.on_clear()
                if inspect.isawaitable(during_release):
                    await during_release
            if state.clear_status is None:
                raise httpx.ReadTimeout("runner did not answer", request=request)
            return httpx.Response(state.clear_status, json={"error": "busy"})
        return httpx.Response(201 if request.url.path == "/v1/sessions" else 200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://runner")
    register_test_runner(app, _RUNNER_ID)
    monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=client))
    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", AsyncMock())
    monkeypatch.setattr(
        routes_core,
        "_publish_session_superseded",
        lambda source, target, *, reason: state.superseded.append((source, target, reason)),
    )
    monkeypatch.setattr(
        routes_core,
        "_announce_session_added",
        lambda _user_id, session_id: state.announced.append(session_id),
    )
    try:
        yield state
    finally:
        app.state.tunnel_registry.deregister(_RUNNER_ID)
        await client.aclose()


@pytest_asyncio.fixture
async def live_runner(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LiveRunner]:
    """A live runner on the shared single-user app."""
    async with install_live_runner(app, monkeypatch) as state:
        yield state


@pytest_asyncio.fixture
async def auth_live_runner(
    auth_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveRunner]:
    """A live runner on the app that enforces session permissions."""
    async with install_live_runner(auth_app, monkeypatch) as state:
        yield state


def resolve_harness_as(monkeypatch: pytest.MonkeyPatch, harness: str) -> None:
    """Make every session resolve to *harness*."""
    monkeypatch.setattr(sessions_module, "_resolve_harness", lambda *_a, **_k: harness)


def set_runner_workspace(db_uri: str, session_id: str, workspace: str, git_branch: str) -> None:
    """Give a runner-only *session_id* a workspace and branch, with no host."""
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlConversationMetadata)
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == session_id,
            )
            .values(workspace=workspace, git_branch=git_branch)
        )


def bind_to_host(
    store: SqlAlchemyConversationStore,
    session_id: str,
    workspace: str,
    git_branch: str | None = None,
) -> None:
    """Bind *session_id* to ``_HOST_ID``, registering the host if it is new."""
    host_store = HostStore(store.storage_location)
    if host_store.get_host(_HOST_ID) is None:
        host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
    store.set_host_id(session_id, _HOST_ID, workspace, git_branch)


async def create_placed_session(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    *,
    runner_id: str | None = _RUNNER_ID,
    host: bool = True,
    user: str | None = None,
    turns: int = 2,
) -> str:
    """Create a session of *turns* turns with run settings and placement.

    :param user: Creating user on an app with permissions, e.g. ``"alice@example.com"``.
    :returns: The session id.
    """
    agent = await create_test_agent(client, user=user, skills=[_REVIEW_SKILL])
    resp = await client.post(
        "/v1/sessions",
        headers={"X-Forwarded-Email": user} if user else None,
        json={
            "agent_id": agent["id"],
            "title": "Parser work",
            "labels": {"team": "core"},
            "model_override": "claude-opus-4-7",
            "reasoning_effort": "high",
            "terminal_launch_args": ["--permission-mode", "acceptEdits"],
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    for turn in range(1, turns + 1):
        store.append(
            session_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{turn}",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": f"Q{turn}"}]
                    ),
                ),
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{turn}",
                    data=MessageData(
                        role="assistant",
                        content=[{"type": "output_text", "text": f"A{turn}"}],
                        agent="test-agent",
                    ),
                ),
            ],
        )
    if host:
        bind_to_host(store, session_id, "/work/repo", "feature/parser")
    if runner_id is not None:
        store.replace_runner_id(session_id, runner_id)
    return session_id


def message_texts(store: SqlAlchemyConversationStore, session_id: str) -> list[str]:
    """Return every message text in *session_id*, in order."""
    return [
        part["text"]
        for item in store.list_items(session_id).data
        if isinstance(item.data, MessageData)
        for part in item.data.content
    ]


def install_managed_sandbox(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch, sandbox_id: str = "sb-clear"
) -> tuple[HostStore, FakeSandboxLauncher]:
    """Make ``_HOST_ID`` the managed sandbox *sandbox_id*, recording its teardown."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id=_HOST_ID,
        name=f"managed-{sandbox_id}",
        user_id=RESERVED_USER_LOCAL,
        token=f"{sandbox_id}-token",
        provider="modal",
        sandbox_id=sandbox_id,
        token_expires_at=int(time.time()) + 3600,
    )
    launcher = FakeSandboxLauncher()
    monkeypatch.setattr(app.state, "host_store", host_store)
    monkeypatch.setattr(
        app.state,
        "sandbox_config",
        ManagedSandboxDeployment.single(
            ManagedSandboxConfig(
                server_url="https://managed-test.example.com",
                launcher_factory=lambda: launcher,
                token_ttl_s=3600,
                provider="modal",
            )
        ),
    )
    return host_store, launcher
