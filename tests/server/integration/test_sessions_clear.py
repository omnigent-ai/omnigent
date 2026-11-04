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
import contextlib
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

import omnigent.runtime
from omnigent.db.db_models import SqlAgent, SqlConversation, current_workspace_id
from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_READ,
)
from omnigent.server.managed_hosts import (
    REQUESTED_HOST_ID_LABEL_KEY,
    managed_repo_labels,
    read_managed_repo_workspaces,
)
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import create_test_agent
from tests.server.integration._session_continuation import (
    _EDITOR,
    _HOST_ID,
    _MESSAGE_INPUT,
    _OWNER,
    _READER,
    _RUNNER_ID,
    LiveRunner,
    bind_to_host,
    create_placed_session,
    install_managed_sandbox,
    message_texts,
    resolve_harness_as,
    set_runner_workspace,
)
from tests.server.integration._session_continuation import (
    auth_live_runner as auth_live_runner,
)
from tests.server.integration._session_continuation import (
    live_runner as live_runner,
)

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

    resolve_harness_as(monkeypatch, harness)

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


def clear_requests(runner: LiveRunner) -> list[dict[str, Any]]:
    """The ``clear`` controls the runner received."""
    return [body for _path, body in runner.posts if body.get("type") == "clear"]


@pytest.mark.parametrize(
    ("runner_status", "turns", "host"),
    [
        pytest.param(200, 2, True, id="release"),
        pytest.param(204, 2, True, id="older-runner"),
        pytest.param(200, 0, True, id="empty-source"),
        pytest.param(200, 2, False, id="runner-only"),
    ],
)
async def test_claude_sdk_clear_moves_to_a_fresh_session_on_the_same_placement(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: int,
    turns: int,
    host: bool,
) -> None:
    """``clear`` frees the old SDK client, then continues in an empty bound session.

    The runner gets a marked request, so only a server that continues the work
    makes it release the client; an older runner's 204 still continues. The new
    session keeps the source's host, workspace, branch, runner, sandbox
    repositories, run settings and routing switches but none of its files; the
    source keeps its history and gets a notice plus a redirect. A source on a
    runner with no host keeps its workspace and branch too. An empty source
    that gains an item mid-clear (e.g. a concurrent clear's notice) still
    yields an empty session.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    live_runner.clear_status = runner_status
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, turns=turns, host=host)
    if not host:
        set_runner_workspace(db_uri, source_id, "/work/repo", "feature/parser")
    written_meanwhile = ["Earlier clear notice"] if turns == 0 else []
    if written_meanwhile:
        live_runner.on_clear = lambda: store.append(
            source_id,
            [
                NewConversationItem(
                    type="message",
                    response_id="resp_notice",
                    data=MessageData(
                        role="assistant",
                        content=[{"type": "output_text", "text": written_meanwhile[0]}],
                        agent="test-agent",
                    ),
                )
            ],
        )
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
    assert store.list_items(new_id).data == []
    assert file_store.list(new_id).data == []
    assert [stored.filename for stored in file_store.list(source_id).data] == ["notes.txt"]
    fork = store.get_conversation(new_id)
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace, fork.git_branch) == (
        _RUNNER_ID,
        _HOST_ID if host else None,
        "/work/repo",
        "feature/parser",
    )
    # The runner starts the new session's turns in that workspace.
    init = next(
        body
        for path, body in live_runner.posts
        if path == "/v1/sessions" and body.get("session_id") == new_id
    )
    assert init["session_init"]["snapshot"]["workspace"] == "/work/repo"
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
    history += written_meanwhile
    assert message_texts(store, source_id)[:-1] == history
    notice = message_texts(store, source_id)[-1]
    assert "ended by `/clear`" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "clear")]
    # Listed in the owner's other tabs once placed.
    assert new_id in live_runner.announced


async def test_claude_sdk_clear_ignores_attachments_in_the_history_it_drops(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file only a native harness can open doesn't stop ``/clear``, which keeps no history.

    The history can hold such a file, e.g. from before the agent was
    reinstalled as claude-sdk; the empty continuation never sees it.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    report = SqlAlchemyFileStore(db_uri).create(
        filename="report.xlsx", bytes=16, session_id=source_id
    )
    store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_3",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_file", "file_id": report.id},
                        {"type": "input_text", "text": "Q3"},
                    ],
                ),
            )
        ],
    )

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 202, resp.text
    assert store.list_items(resp.json()["session_id"]).data == []
    assert len(clear_requests(live_runner)) == 1


@pytest.mark.parametrize("cancelled_during", ["fork", "runner_binding"])
async def test_a_cancelled_clear_request_still_finishes_the_move(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    cancelled_during: str,
) -> None:
    """Cancelling the request (e.g. a client disconnect) mid-move leaves no half-made session.

    Once allowed, the move runs to its end on its own: exactly one new session
    is placed and announced, a retry is refused while it runs, and the original
    is superseded but can still be resumed.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    before = len((await client.get("/v1/sessions")).json()["data"])
    loop = asyncio.get_running_loop()
    paused, resume = asyncio.Event(), threading.Event()
    if cancelled_during == "fork":
        fork = SqlAlchemyConversationStore.fork_conversation

        def fork_after_cancel(self: Any, *args: Any, **kwargs: Any) -> Any:
            loop.call_soon_threadsafe(paused.set)
            assert resume.wait(timeout=10)
            return fork(self, *args, **kwargs)

        monkeypatch.setattr(SqlAlchemyConversationStore, "fork_conversation", fork_after_cancel)
    else:
        resolve_runner = sessions_module._get_runner_client

        async def bind_after_cancel(session_id: str, *args: Any, **kwargs: Any) -> Any:
            if session_id != source_id and not paused.is_set():
                paused.set()
                await asyncio.to_thread(resume.wait, 10)
            return await resolve_runner(session_id, *args, **kwargs)

        monkeypatch.setattr(sessions_module, "_get_runner_client", bind_after_cancel)
    clearing = asyncio.create_task(
        client.post(f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}})
    )
    await asyncio.wait_for(paused.wait(), timeout=10)

    clearing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await clearing
    retried = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )
    resume.set()
    await poll_until_async(lambda: bool(live_runner.superseded), timeout_s=10)

    assert retried.status_code == 409, retried.text
    [(_, new_id, _)] = live_runner.superseded
    continuation = store.get_conversation(new_id)
    assert continuation is not None
    assert (continuation.runner_id, continuation.host_id) == (_RUNNER_ID, _HOST_ID)
    assert new_id in live_runner.announced
    assert len((await client.get("/v1/sessions")).json()["data"]) == before + 1
    assert not orchestration._session_is_moving(source_id)
    resumed = await client.post(f"/v1/sessions/{source_id}/events", json=_MESSAGE_INPUT)
    assert resumed.status_code < 300, resumed.text


def _agent_ids(db_uri: str) -> set[str]:
    """Every agent row in the workspace, session copies included."""
    with get_or_create_engine(db_uri).connect() as connection:
        rows = connection.execute(
            select(SqlAgent.id).where(SqlAgent.workspace_id == current_workspace_id())
        )
        return {row.id for row in rows}


def _stored_files(root: Path) -> list[Path]:
    """Every file in a local artifact store; deletes leave empty folders."""
    return sorted(path for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize("agent_owner", ["caller", "another_user"])
async def test_a_continuation_that_cannot_be_shared_leaves_no_host_reference(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    auth_live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    agent_owner: str,
) -> None:
    """A failure right after the new session is written still removes it.

    Granting the new session its sharing fails; the clear then deletes it, so
    nothing but the original holds the managed sandbox, and deleting the
    original still tears the sandbox down. When the session runs another
    user's agent, the clear made the caller their own copy of it; that copy
    and its bundle go too.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    host_store, launcher = install_managed_sandbox(auth_app, db_uri, monkeypatch)
    source_id = await create_placed_session(auth_client, store, user=_OWNER)
    owner = {"X-Forwarded-Email": _OWNER}
    if agent_owner == "another_user":
        shared_agent = await create_test_agent(auth_client, name="shared-agent", user=_EDITOR)
        with get_or_create_engine(db_uri).begin() as connection:
            connection.execute(
                update(SqlConversation)
                .where(
                    SqlConversation.workspace_id == current_workspace_id(),
                    SqlConversation.id == source_id,
                )
                .values(agent_id=shared_agent["id"])
            )
    agents_before = _agent_ids(db_uri)
    bundles_before = _stored_files(tmp_path / "artifacts")
    grant = SqlAlchemyPermissionStore.grant

    def grant_on_the_original_only(self: Any, user_id: str, session_id: str, level: int) -> Any:
        if session_id != source_id:
            raise OmnigentError("grant failed", code=ErrorCode.INTERNAL_ERROR)
        return grant(self, user_id, session_id, level)

    monkeypatch.setattr(SqlAlchemyPermissionStore, "grant", grant_on_the_original_only)

    cleared = await auth_client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}, headers=owner
    )

    assert cleared.status_code == 500, cleared.text
    assert [
        conv.id
        for conv in store.list_conversations(limit=100, kind=None).data
        if conv.host_id == _HOST_ID
    ] == [source_id]
    # No copied agent, nor its bundle, outlives the failed clear.
    assert _agent_ids(db_uri) == agents_before
    assert _stored_files(tmp_path / "artifacts") == bundles_before
    assert auth_live_runner.superseded == []
    deleted = await auth_client.delete(f"/v1/sessions/{source_id}", headers=owner)
    assert deleted.status_code == 200, deleted.text
    assert launcher.terminated == ["sb-clear"]
    assert host_store.get_host(_HOST_ID) is None


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


async def test_claude_sdk_clear_re_addresses_a_runner_that_moved_before_release(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that moved to another replica before the release is re-addressed.

    It is still registered here but unreachable, and its host is live
    elsewhere. The offline and other-replica cases shared with ``/rewind`` are
    in ``test_sessions_continuation.py``.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
    monkeypatch.setattr(app.state, "host_store", host_store)
    monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=None))
    monkeypatch.setattr(omnigent.runtime, "get_runner_client", lambda: None)
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}
    )

    assert resp.status_code == 400, resp.text
    assert "wrong_replica" in resp.text
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert clear_requests(live_runner) == []
    assert live_runner.superseded == []


async def test_claude_sdk_clear_removes_a_session_its_runner_will_not_initialize(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation whose runner rejects it leaves nothing behind.

    The source's saved inference configuration must be accepted, so an
    unconfirmed initialization fails the clear. The new session is deleted
    and dropped from the runner it was sent to (which may have set it up
    without confirming), was never listed in the owner's tabs, and the source
    gets no notice or redirect. A failure after the runner set the session up
    is shared with ``/rewind`` and covered in ``test_sessions_continuation.py``.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client)
    source = store.create_conversation(
        agent_id=agent["id"],
        inference_snapshot={"runtime_config": {}, "harness": "claude-sdk", "owner_id": "local"},
    )
    bind_to_host(store, source.id, "/work/repo")
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
    [new_id] = [session_id for session_id in initialized if session_id != source.id]
    assert store.get_conversation(new_id) is None
    # Dropped from the runner even when the runner never confirmed it.
    assert (f"/v1/sessions/{new_id}", {}) in live_runner.posts
    assert new_id not in live_runner.announced
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert message_texts(store, source.id) == []
    assert live_runner.superseded == []


@pytest.mark.parametrize("files_shared", [True, False])
async def test_claude_sdk_clear_continuation_keeps_the_original_file_sharing(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    auth_live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    files_shared: bool,
) -> None:
    """A reader sees the continuation's workspace files exactly when they saw the original's."""
    resolve_harness_as(monkeypatch, "claude-sdk")
    # File reads reach the same live runner.
    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        sessions_module._get_runner_client,
    )
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(auth_client, store, user=_OWNER)
    owner, reader = {"X-Forwarded-Email": _OWNER}, {"X-Forwarded-Email": _READER}
    shared = await auth_client.put(
        f"/v1/sessions/{source_id}/permissions",
        json={"user_id": _READER, "level": LEVEL_READ},
        headers=owner,
    )
    assert shared.status_code < 300, shared.text
    if files_shared:
        opened_files = await auth_client.patch(
            f"/v1/sessions/{source_id}", json={"share_workspace_files": True}, headers=owner
        )
        assert opened_files.status_code == 200, opened_files.text
    changes = "/v1/sessions/{}/resources/environments/default/changes"
    expected = 200 if files_shared else 403
    original = await auth_client.get(changes.format(source_id), headers=reader)
    assert original.status_code == expected, original.text

    cleared = await auth_client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}, headers=owner
    )

    assert cleared.status_code == 202, cleared.text
    continued = await auth_client.get(changes.format(cleared.json()["session_id"]), headers=reader)
    assert continued.status_code == expected, continued.text


@pytest.mark.parametrize(
    ("clear_status", "placement", "status", "message"),
    [
        (409, "runner", 400, "Stop the current turn"),
        (503, "runner", 500, "Clear failed: runner returned 503"),
        (None, "runner", 503, "Couldn't reach this session's runner"),
        (200, "editor", 403, ""),
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

    A busy, failing or unreachable runner, or a non-owner, refuses it. The
    caller check runs before the runner frees the client, so only a runner
    refusal reaches the runner. Refusals shared with ``/rewind`` are in
    ``test_sessions_continuation.py``.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    auth_live_runner.clear_status = clear_status
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(auth_client, store, user=_OWNER)
    caller = _OWNER
    if placement == "editor":
        granted = await auth_client.put(
            f"/v1/sessions/{source_id}/permissions",
            json={"user_id": _EDITOR, "level": LEVEL_EDIT},
            headers={"X-Forwarded-Email": _OWNER},
        )
        assert granted.status_code < 300, granted.text
        caller = _EDITOR
    headers = {"X-Forwarded-Email": caller}
    before = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    posts_before = len(auth_live_runner.posts)

    resp = await auth_client.post(
        f"/v1/sessions/{source_id}/events",
        json={"type": "clear", "data": {}},
        headers=headers,
    )

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
