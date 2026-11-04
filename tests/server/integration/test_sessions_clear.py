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

import asyncio
import inspect
import json
import threading
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

import omnigent.runtime
from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.managed_hosts import (
    REQUESTED_HOST_ID_LABEL_KEY,
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    managed_repo_labels,
    read_managed_repo_workspaces,
)
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_core, routes_events
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import FakeSandboxLauncher, create_test_agent, register_test_runner

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
_REVIEW_SKILL = {"name": "review", "description": "Review the change.", "content": "Review it."}
_SKILL_INPUT = {"type": "slash_command", "data": {"kind": "skill", "name": "review"}}
_MESSAGE_INPUT = {
    "type": "message",
    "data": {"role": "user", "content": [{"type": "input_text", "text": "Q3"}]},
}
_OWNER = "alice@example.com"
_EDITOR = "bob@example.com"
_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


@dataclass
class LiveRunner:
    """What the fake live runner received, and how it answers ``clear``."""

    # ``None`` times the ``clear`` out at the transport.
    clear_status: int | None = 200
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    superseded: list[tuple[str, str, str]] = field(default_factory=list)
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


def clear_requests(runner: LiveRunner) -> list[dict[str, Any]]:
    """The ``clear`` controls the runner received."""
    return [body for _path, body in runner.posts if body.get("type") == "clear"]


def resolve_harness_as(monkeypatch: pytest.MonkeyPatch, harness: str) -> None:
    """Make every session resolve to *harness*."""
    monkeypatch.setattr(sessions_module, "_resolve_harness", lambda *_a, **_k: harness)


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
        store.set_host_id(session_id, _HOST_ID, "/work/repo", "feature/parser")
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


@pytest.mark.parametrize(
    ("runner_status", "turns"),
    [
        pytest.param(200, 2, id="release"),
        pytest.param(204, 2, id="older-runner"),
        pytest.param(200, 0, id="empty-source"),
    ],
)
async def test_claude_sdk_clear_moves_to_a_fresh_session_on_the_same_placement(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: int,
    turns: int,
) -> None:
    """``clear`` frees the old SDK client, then continues in an empty bound session.

    The runner gets a marked request, so only a server that continues the work
    makes it release the client; an older runner's 204 still continues. The new
    session keeps the source's host, workspace, branch, runner, sandbox
    repositories, run settings and routing switches but none of its files; the source keeps its
    history and gets a notice plus a redirect.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    live_runner.clear_status = runner_status
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, turns=turns)
    file_store = SqlAlchemyFileStore(db_uri)
    file_store.create("notes.txt", bytes=5, session_id=source_id)
    repos = ["https://github.com/acme/api", "https://github.com/acme/web#main"]
    sandbox_labels = {**managed_repo_labels(repos), REQUESTED_HOST_ID_LABEL_KEY: _HOST_ID}
    store.set_labels(source_id, sandbox_labels)
    store.update_conversation(
        source_id, cost_control_mode_override="on", subagent_routing_override="on"
    )

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    new_id = resp.json()["session_id"]
    release = (
        f"/v1/sessions/{source_id}/events",
        {"type": "clear", "continue_in_new_session": True},
    )
    # The old session's client is freed before the new session is initialized.
    initialized = [(path, body.get("session_id")) for path, body in live_runner.posts]
    assert live_runner.posts.index(release) < initialized.index(("/v1/sessions", new_id))
    assert message_texts(store, new_id) == []
    assert file_store.list(new_id).data == []
    assert [stored.filename for stored in file_store.list(source_id).data] == ["notes.txt"]
    fork = store.get_conversation(new_id)
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace, fork.git_branch) == (
        _RUNNER_ID,
        _HOST_ID,
        "/work/repo",
        "feature/parser",
    )
    assert (fork.model_override, fork.reasoning_effort) == ("claude-opus-4-7", "high")
    assert (fork.cost_control_mode_override, fork.subagent_routing_override) == ("on", "on")
    assert fork.terminal_launch_args == ["--permission-mode", "acceptEdits"]
    assert fork.labels["team"] == "core"
    # A relaunch of the shared sandbox still re-clones the repositories.
    assert {key: fork.labels.get(key) for key in sandbox_labels} == sandbox_labels
    assert read_managed_repo_workspaces(fork.labels) == repos
    # Untitled, so the first message names it like any new chat.
    assert fork.title is None
    history = [text for turn in range(1, turns + 1) for text in (f"Q{turn}", f"A{turn}")]
    assert message_texts(store, source_id)[:-1] == history
    notice = message_texts(store, source_id)[-1]
    assert "ended by `/clear`" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "clear")]


async def test_claude_sdk_clear_copies_no_item_written_while_it_runs(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty source that gains an item mid-clear still yields an empty session.

    A concurrent clear's notice can land in the source after this clear has
    looked at it; the new session must not inherit it.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, turns=0)
    live_runner.on_clear = lambda: store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_notice",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Earlier clear notice"}],
                    agent="test-agent",
                ),
            )
        ],
    )

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    assert message_texts(store, source_id)[0] == "Earlier clear notice"
    assert store.list_items(resp.json()["session_id"]).data == []


async def test_claude_sdk_clear_refuses_a_message_sent_while_it_moves_the_session(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message, skill or second ``/clear`` sent mid-move, after the release, is refused.

    The message or skill would otherwise run in the original chat while
    viewers follow the new, empty one; the second clear would release and fork
    again. The first clear still completes, and the guard ends with it.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    sent: list[httpx.Response] = []
    second_clear: list[httpx.Response] = []

    async def act_from_another_tab() -> None:
        # Only the first release acts; a second clear reaching its own release
        # (when unguarded) must not recurse.
        live_runner.on_clear = None
        second_clear.append(
            await client.post(
                f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
            )
        )
        for turn_input in (_MESSAGE_INPUT, _SKILL_INPUT):
            sent.append(await client.post(f"/v1/sessions/{source_id}/events", json=turn_input))

    live_runner.on_clear = act_from_another_tab
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    new_id = resp.json()["session_id"]
    assert [response.status_code for response in sent] == [409, 409], [r.text for r in sent]
    assert all("moving to a new session" in response.text for response in sent)
    assert [response.status_code for response in second_clear] == [409], second_clear[0].text
    assert "already moving" in second_clear[0].text
    assert len(clear_requests(live_runner)) == 1
    assert len((await client.get("/v1/sessions")).json()["data"]) == before + 1
    assert not orchestration._session_is_moving(source_id)
    started = [body for path, body in live_runner.posts if body.get("type") == "message"]
    assert started == []
    assert "Q3" not in message_texts(store, source_id)
    assert live_runner.superseded == [(source_id, new_id, "clear")]
    # The guard ends with the move: the original chat takes messages again.
    resumed = await client.post(
        f"/v1/sessions/{source_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "Q4"}]},
        },
    )
    assert resumed.status_code < 300, resumed.text


def install_managed_sandbox(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[HostStore, FakeSandboxLauncher]:
    """Make ``_HOST_ID`` a managed sandbox host whose teardown is recorded."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id=_HOST_ID,
        name="managed-clear",
        user_id=RESERVED_USER_LOCAL,
        token="managed-clear-token",
        provider="modal",
        sandbox_id="sb-clear",
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


@pytest.mark.parametrize("deleted_first", ["original", "continuation"])
async def test_deleting_one_side_of_a_clear_keeps_their_shared_sandbox(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    deleted_first: str,
) -> None:
    """Both sessions of a ``/clear`` work in one managed sandbox.

    Deleting either leaves the other bound to the host and the sandbox
    running; deleting the last one still tears the sandbox down.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    host_store, launcher = install_managed_sandbox(app, db_uri, monkeypatch)
    source_id = await create_placed_session(client, store)
    cleared = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )
    assert cleared.status_code == 202, cleared.text
    new_id = cleared.json()["session_id"]
    first, last = (source_id, new_id) if deleted_first == "original" else (new_id, source_id)

    deleted = await client.delete(f"/v1/sessions/{first}")

    assert deleted.status_code == 200, deleted.text
    survivor = store.get_conversation(last)
    assert survivor is not None and survivor.host_id == _HOST_ID
    assert host_store.get_host(_HOST_ID) is not None
    assert launcher.terminated == []

    deleted = await client.delete(f"/v1/sessions/{last}")

    assert deleted.status_code == 200, deleted.text
    assert launcher.terminated == ["sb-clear"]
    assert host_store.get_host(_HOST_ID) is None


async def test_stopping_or_archiving_the_original_leaves_the_continuation_running(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleared original no longer holds the runner it shares with its continuation.

    Stopping or archiving the original ends only that chat: the runner is not
    stopped under the continuation's running turn, and the original keeps its
    host so a later message can resume it.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    cleared = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )
    assert cleared.status_code == 202, cleared.text
    new_id = cleared.json()["session_id"]
    monkeypatch.setitem(sessions_module._session_status_cache, new_id, "running")
    runner_stops = AsyncMock(return_value=True)
    monkeypatch.setattr(routes_events, "_stop_host_runner_intentionally", runner_stops)
    monkeypatch.setattr(orchestration, "_stop_host_runner_intentionally", runner_stops)
    monkeypatch.setattr(sessions_module, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0)

    stopped = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "stop_session", "data": {}}
    )
    archived = await client.patch(f"/v1/sessions/{source_id}", json={"archived": True})
    await asyncio.gather(*list(orchestration._detached_stop_tasks), return_exceptions=True)

    assert stopped.status_code < 300, stopped.text
    assert archived.status_code == 200, archived.text
    runner_stops.assert_not_awaited()
    continuation = store.get_conversation(new_id)
    assert continuation is not None and continuation.runner_id == _RUNNER_ID
    assert sessions_module._session_status_cache.get(new_id) == "running"
    original = store.get_conversation(source_id)
    assert original is not None
    assert (original.runner_id, original.host_id) == (None, _HOST_ID)


@pytest.mark.parametrize("deleted", ["before_fork", "after_fork"])
async def test_deleting_the_original_during_the_handoff_never_strands_the_continuation(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    deleted: str,
) -> None:
    """A delete of the original racing the clear never leaves a sandbox-less continuation.

    Once the continuation exists it already holds the host, so the delete keeps
    the sandbox; a delete that lands first leaves no continuation behind.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    host_store, launcher = install_managed_sandbox(app, db_uri, monkeypatch)
    source_id = await create_placed_session(client, store)
    before = len((await client.get("/v1/sessions")).json()["data"])
    loop = asyncio.get_running_loop()
    paused, resume = asyncio.Event(), threading.Event()
    fork = SqlAlchemyConversationStore.fork_conversation

    def fork_around_the_delete(self: Any, *args: Any, **kwargs: Any) -> Any:
        forked = fork(self, *args, **kwargs) if deleted == "after_fork" else None
        loop.call_soon_threadsafe(paused.set)
        assert resume.wait(timeout=10)
        return forked if forked is not None else fork(self, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyConversationStore, "fork_conversation", fork_around_the_delete)
    clearing = asyncio.create_task(
        client.post(f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}})
    )
    await asyncio.wait_for(paused.wait(), timeout=10)
    removed = await client.delete(f"/v1/sessions/{source_id}")
    resume.set()
    cleared = await asyncio.wait_for(clearing, timeout=10)

    assert removed.status_code == 200, removed.text
    if deleted == "before_fork":
        assert cleared.status_code >= 400, cleared.text
        assert len((await client.get("/v1/sessions")).json()["data"]) == before - 1
        assert launcher.terminated == ["sb-clear"]
        return
    assert cleared.status_code == 202, cleared.text
    continuation = store.get_conversation(cleared.json()["session_id"])
    assert continuation is not None and continuation.host_id == _HOST_ID
    assert host_store.get_host(_HOST_ID) is not None
    assert launcher.terminated == []


async def test_claude_sdk_clear_still_redirects_when_the_notice_is_lost(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed notice write still returns and announces the new session.

    The notice is best-effort, so the client follows the session that already
    holds the work instead of a retry making a second one.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    monkeypatch.setattr(
        routes_core,
        "_persist_external_conversation_item",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    new_id = resp.json()["session_id"]
    assert store.get_conversation(new_id) is not None
    assert live_runner.superseded == [(source_id, new_id, "clear")]
    assert message_texts(store, source_id) == ["Q1", "A1", "Q2", "A2"]


@pytest.mark.parametrize(
    ("runner_id", "host_live", "runner_reachable"),
    [
        pytest.param("runner_gone", False, True, id="offline"),
        pytest.param("runner_gone", True, True, id="on-another-replica"),
        pytest.param(_RUNNER_ID, True, False, id="moved-before-release"),
    ],
)
async def test_claude_sdk_clear_tells_an_offline_runner_from_one_on_another_replica(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    runner_id: str,
    host_live: bool,
    runner_reachable: bool,
) -> None:
    """An offline runner leaves the new session on the host, which relaunches it on send.

    A runner absent here whose host is live elsewhere is on another replica, so
    ``clear`` re-addresses there before anything is created.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, runner_id=runner_id)
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
    if not host_live:
        host_store.set_offline(_HOST_ID)
    monkeypatch.setattr(app.state, "host_store", host_store)
    if not runner_reachable:
        monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=None))
        monkeypatch.setattr(omnigent.runtime, "get_runner_client", lambda: None)
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    if host_live:
        assert resp.status_code == 400, resp.text
        assert "wrong_replica" in resp.text
        assert len((await client.get("/v1/sessions")).json()["data"]) == before
        assert clear_requests(live_runner) == []
        assert live_runner.superseded == []
        return
    assert resp.status_code == 202, resp.text
    fork = store.get_conversation(resp.json()["session_id"])
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace) == (None, _HOST_ID, "/work/repo")


@pytest.mark.parametrize("host", [True, False], ids=["host-bound", "runner-only"])
@pytest.mark.parametrize("left", ["before_validation", "after_validation", "to_another_replica"])
async def test_claude_sdk_clear_survives_the_runner_leaving_before_it_is_bound(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    host: bool,
    left: str,
) -> None:
    """A runner that disconnects after the check leaves no half-made session.

    Whether it leaves before the new session's runner is validated or just
    after (when binding still reports success), a host-bound session continues
    on the host alone; otherwise the new session is removed, so a retry does
    not leave a second copy. A runner that reconnected to another replica is
    re-addressed there instead.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, host=host)
    if left == "after_validation":
        resolve_runner = sessions_module._get_runner_client

        async def lose_runner_at_lookup(session_id: str, *args: Any, **kwargs: Any) -> Any:
            if session_id != source_id:
                app.state.tunnel_registry.deregister(_RUNNER_ID)
                return None
            return await resolve_runner(session_id, *args, **kwargs)

        monkeypatch.setattr(sessions_module, "_get_runner_client", lose_runner_at_lookup)
    else:
        live_runner.on_clear = lambda: app.state.tunnel_registry.deregister(_RUNNER_ID)
    if left == "to_another_replica":
        host_store = HostStore(db_uri)
        host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
        monkeypatch.setattr(app.state, "host_store", host_store)
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    if host and left == "to_another_replica":
        assert resp.status_code == 400, resp.text
        assert "wrong_replica" in resp.text
    elif host:
        assert resp.status_code == 202, resp.text
        new_id = resp.json()["session_id"]
        fork = store.get_conversation(new_id)
        assert fork is not None
        assert (fork.host_id, fork.workspace) == (_HOST_ID, "/work/repo")
        assert live_runner.superseded == [(source_id, new_id, "clear")]
        return
    else:
        assert resp.status_code == 503, resp.text
        assert "runner is offline" in resp.text
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert live_runner.superseded == []


async def test_claude_sdk_clear_removes_a_session_its_runner_will_not_initialize(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered runner that rejects the new session leaves nothing behind.

    The source's saved inference configuration must be accepted, so the
    unconfirmed initialization fails the clear; the new session is deleted
    and the source gets no notice or redirect.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client)
    source = store.create_conversation(
        agent_id=agent["id"],
        inference_snapshot={"runtime_config": {}, "harness": "claude-sdk", "owner_id": "local"},
    )
    store.set_host_id(source.id, _HOST_ID, "/work/repo")
    store.replace_runner_id(source.id, _RUNNER_ID)
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source.id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 503, resp.text
    assert "did not accept this session's saved inference configuration" in resp.text
    initialized = [
        body["session_id"] for path, body in live_runner.posts if path == "/v1/sessions"
    ]
    assert [session_id for session_id in initialized if session_id != source.id]
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert message_texts(store, source.id) == []
    assert live_runner.superseded == []


@pytest.mark.parametrize(
    ("clear_status", "placement", "status", "message"),
    [
        (409, "runner", 400, "Stop the current turn"),
        (503, "runner", 500, "Clear failed: runner returned 503"),
        (None, "runner", 503, "Couldn't reach this session's runner"),
        (200, "running", 400, "Stop the current turn"),
        (200, "starting", 400, "Stop the current turn"),
        (200, "skill_starting", 400, "Stop the current turn"),
        (200, "failed_starting", 400, "Stop the current turn"),
        (200, "none", 503, "runner is offline"),
        (200, "editor", 403, ""),
        (200, "sub_agent", 400, "only available in a top-level session"),
        (200, "side_chat", 400, "isn't available in a side chat"),
    ],
)
async def test_claude_sdk_clear_refusals_create_no_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    auth_live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    clear_status: int | None,
    placement: str,
    status: int,
    message: str,
) -> None:
    """A refused ``clear`` creates, initializes and supersedes nothing.

    A busy, failing or unreachable runner, a running turn or one still
    reaching its runner, no placement, a non-owner, a sub-agent or a side chat refuses it.
    The caller and session checks run before the runner frees the client, so
    only a runner refusal reaches the runner.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    auth_live_runner.clear_status = clear_status
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(
        auth_client,
        store,
        runner_id=None
        if placement == "none"
        else "runner_gone"
        if placement.endswith("starting")
        else _RUNNER_ID,
        host=placement != "none",
        user=_OWNER,
    )
    caller = _OWNER
    message_task: asyncio.Task[httpx.Response] | None = None
    release_message = asyncio.Event()
    if placement.endswith("starting"):
        # An input to the offline runner waits on runner recovery; a failed
        # session still shows "failed" while its recovery input is pending.
        if placement == "failed_starting":
            monkeypatch.setitem(sessions_module._session_status_cache, source_id, "failed")
        recovering = asyncio.Event()
        resolve_runner = sessions_module._get_runner_client

        async def recover_runner(*args: Any, **kwargs: Any) -> Any:
            if not recovering.is_set():
                recovering.set()
                await release_message.wait()
            return await resolve_runner(*args, **kwargs)

        monkeypatch.setattr(sessions_module, "_get_runner_client", recover_runner)
        message_task = asyncio.create_task(
            auth_client.post(
                f"/v1/sessions/{source_id}/events",
                json=_SKILL_INPUT if placement == "skill_starting" else _MESSAGE_INPUT,
                headers={"X-Forwarded-Email": _OWNER},
            )
        )
        await asyncio.wait_for(recovering.wait(), timeout=10)
    elif placement == "running":
        monkeypatch.setitem(sessions_module._session_status_cache, source_id, "running")
    elif placement == "editor":
        granted = await auth_client.put(
            f"/v1/sessions/{source_id}/permissions",
            json={"user_id": _EDITOR, "level": LEVEL_EDIT},
            headers={"X-Forwarded-Email": _OWNER},
        )
        assert granted.status_code < 300, granted.text
        caller = _EDITOR
    elif placement == "sub_agent":
        source = store.get_conversation(source_id)
        assert source is not None
        child = store.create_conversation(
            kind="sub_agent", parent_conversation_id=source_id, agent_id=source.agent_id
        )
        store.set_host_id(child.id, _HOST_ID, "/work/repo")
        store.replace_runner_id(child.id, _RUNNER_ID)
        SqlAlchemyPermissionStore(db_uri).grant(_OWNER, child.id, LEVEL_OWNER)
        source_id = child.id
    elif placement == "side_chat":
        # A side chat rides its source's runner and has no host of its own.
        side_chat = store.create_conversation(
            agent_id=store.get_conversation(source_id).agent_id,
            labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source_id},
        )
        store.replace_runner_id(side_chat.id, _RUNNER_ID)
        SqlAlchemyPermissionStore(db_uri).grant(_OWNER, side_chat.id, LEVEL_OWNER)
        source_id = side_chat.id
    headers = {"X-Forwarded-Email": caller}
    before = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    posts_before = len(auth_live_runner.posts)

    try:
        resp = await auth_client.post(
            f"/v1/sessions/{source_id}/events",
            json={"type": "clear", "data": {}},
            headers=headers,
        )
    finally:
        release_message.set()
        if message_task is not None:
            await message_task

    assert resp.status_code == status, resp.text
    assert message in resp.text
    after = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    assert after == before
    assert auth_live_runner.superseded == []
    # No new session was initialized on the runner.
    assert "/v1/sessions" not in [path for path, _body in auth_live_runner.posts[posts_before:]]
    if placement != "runner":
        assert clear_requests(auth_live_runner) == []
    # A refused clear leaves the session open to messages.
    assert not orchestration._session_is_moving(source_id)
