"""Integration tests for the explicit ``clear`` control event.

The web-UI ``/clear`` command POSTs ``{"type": "clear"}`` to
``POST /v1/sessions/{id}/events``. Like ``compact``, the server forwards the
control to the bound runner, which types the harness's own ``/clear`` into the
session terminal (200), no-ops for harnesses without one (204), or reports a
failed injection (5xx). A claude-sdk session instead continues in a fresh
session on the same host, workspace and runner, after the runner frees its live
SDK client.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent, register_test_runner

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a bare session bound to *agent_id* and return its id.

    :param client: The test HTTP client.
    :param agent_id: Agent id to bind, e.g. ``"ag_abc123"``.
    :returns: The new session id, e.g. ``"conv_abc123"``.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id, "initial_items": []})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _fake_runner_returning(clear_status: int) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """
    Build a mock runner client that returns *clear_status* for ``clear``.

    :param clear_status: HTTP status for a forwarded ``clear`` control, e.g.
        ``200`` (native handled), ``204`` (no-op) or ``503`` (pane not ready).
    :returns: The mock client and the list capturing forwarded clear bodies.
    """
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record clear POSTs and return the configured status."""
        body = (
            json.loads(request.content) if request.method == "POST" and request.content else None
        )
        if isinstance(body, dict) and body.get("type") == "clear":
            captured.append(body)
            return httpx.Response(clear_status)
        return httpx.Response(204)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    ), captured


async def _post_clear(
    client: httpx.AsyncClient,
    clear_status: int,
    monkeypatch: pytest.MonkeyPatch,
    harness: str = "claude-native",
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    """
    POST a ``clear`` control with the runner answering *clear_status*.

    :param client: The test HTTP client.
    :param clear_status: Status the fake runner returns for the control.
    :param monkeypatch: Pins the session's resolved harness.
    :param harness: Harness the session resolves to. claude-sdk ``clear``
        moves to a fresh session instead (the ``test_claude_sdk_clear_*`` tests below).
    :returns: The server response and the forwarded clear bodies.
    """
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_resolve_harness", lambda *_a, **_k: harness)

    runner, captured = _fake_runner_returning(clear_status)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "clear", "data": {}})
    finally:
        await runner.aclose()
        set_runner_client(None)
    return resp, captured


async def test_clear_is_forwarded_to_the_runner(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner that typed the harness's /clear makes the control succeed."""
    resp, captured = await _post_clear(client, 200, monkeypatch)

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}
    assert captured == [{"type": "clear"}]


async def test_clear_reports_unsupported_session_types(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner no-op (harness without a native /clear) is a clear 400."""
    resp, captured = await _post_clear(client, 204, monkeypatch, harness="openai-agents")

    assert resp.status_code == 400, resp.text
    assert "/clear is not available" in resp.text
    assert captured == [{"type": "clear"}]


async def test_clear_surfaces_a_failed_injection(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner that could not type /clear surfaces an error, not success."""
    resp, captured = await _post_clear(client, 503, monkeypatch)

    assert resp.status_code >= 500, resp.text
    assert "Clear failed" in resp.text
    assert captured == [{"type": "clear"}]


_RUNNER_ID = "runner_live"
_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


@dataclass
class LiveRunner:
    """What the fake live runner received, and how it answers ``clear``."""

    clear_status: int = 200
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    superseded: list[tuple[str, str, str]] = field(default_factory=list)


@pytest_asyncio.fixture
async def live_runner(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LiveRunner]:
    """Register a live runner and record what the server sends it."""
    state = LiveRunner()

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        state.posts.append((request.url.path, body))
        if body.get("type") == "clear":
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
    yield state
    app.state.tunnel_registry.deregister(_RUNNER_ID)
    await client.aclose()


def resolve_harness_as(monkeypatch: pytest.MonkeyPatch, harness: str) -> None:
    """Make every session resolve to *harness*."""
    monkeypatch.setattr(sessions_module, "_resolve_harness", lambda *_a, **_k: harness)


async def create_placed_session(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    *,
    runner_id: str | None = _RUNNER_ID,
    host: bool = True,
) -> tuple[str, list[str]]:
    """Create a two-turn session with run settings and placement.

    :returns: The session id and its item ids (Q1, A1, Q2, A2).
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
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
    for turn in (1, 2):
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
        store.set_host_id(session_id, _HOST_ID, "/work/repo", "feature/parser")
    if runner_id is not None:
        store.replace_runner_id(session_id, runner_id)
    return session_id, [item.id for item in store.list_items(session_id).data]


def message_texts(store: SqlAlchemyConversationStore, session_id: str) -> list[str]:
    """Return every message text in *session_id*, in order."""
    return [
        part["text"]
        for item in store.list_items(session_id).data
        if isinstance(item.data, MessageData)
        for part in item.data.content
    ]


async def test_claude_sdk_clear_moves_to_a_fresh_session_on_the_same_placement(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``clear`` frees the old SDK client, then continues in an empty bound session.

    The new session keeps the source's host, workspace, branch, runner and run
    settings; the source keeps its history and gets a notice plus a redirect.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id, _items = await create_placed_session(client, store)

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    new_id = resp.json()["session_id"]
    sent = [(path, body.get("type") or body.get("session_id")) for path, body in live_runner.posts]
    # The old session's client is freed before the new session is initialized.
    assert sent.index((f"/v1/sessions/{source_id}/events", "clear")) < sent.index(
        ("/v1/sessions", new_id)
    )
    assert message_texts(store, new_id) == []
    fork = store.get_conversation(new_id)
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace, fork.git_branch) == (
        _RUNNER_ID,
        _HOST_ID,
        "/work/repo",
        "feature/parser",
    )
    assert (fork.model_override, fork.reasoning_effort) == ("claude-opus-4-7", "high")
    assert fork.terminal_launch_args == ["--permission-mode", "acceptEdits"]
    assert fork.labels["team"] == "core"
    # Untitled, so the first message names it like any new chat.
    assert fork.title is None
    assert message_texts(store, source_id)[:4] == ["Q1", "A1", "Q2", "A2"]
    notice = message_texts(store, source_id)[-1]
    assert "ended by `/clear`" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "clear")]


async def test_claude_sdk_clear_with_an_offline_runner_binds_the_host_only(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped runner leaves the new session on the host, which relaunches it on send."""
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id, _items = await create_placed_session(client, store, runner_id="runner_gone")

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    fork = store.get_conversation(resp.json()["session_id"])
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace) == (None, _HOST_ID, "/work/repo")


@pytest.mark.parametrize(
    ("clear_status", "placement", "status", "message"),
    [
        (409, "runner", 400, "Stop the current turn"),
        (200, "running", 400, "Stop the current turn"),
        (200, "none", 503, "runner is offline"),
    ],
)
async def test_claude_sdk_clear_refusals_create_no_session(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    clear_status: int,
    placement: str,
    status: int,
    message: str,
) -> None:
    """A busy runner or turn, or no runner and no host, refuses ``clear``."""
    resolve_harness_as(monkeypatch, "claude-sdk")
    live_runner.clear_status = clear_status
    store = SqlAlchemyConversationStore(db_uri)
    source_id, _items = await create_placed_session(
        client,
        store,
        runner_id=None if placement == "none" else _RUNNER_ID,
        host=placement != "none",
    )
    if placement == "running":
        monkeypatch.setitem(sessions_module._session_status_cache, source_id, "running")
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == status, resp.text
    assert message in resp.text
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert live_runner.superseded == []
