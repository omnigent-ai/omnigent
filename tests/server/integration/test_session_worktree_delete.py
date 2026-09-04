"""
Integration tests for opt-in git worktree cleanup on session delete.

Drives ``DELETE /v1/sessions/{id}`` through the full app with a fake
host registered in ``app.state.host_registry``. Verifies the
``?delete_branch`` flag gates whether a ``host.remove_worktree`` frame
is sent, and that the stored worktree path + branch (not request input)
are used. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostRemoveWorktreeFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "a65b7d8e4613a95946c9134383308ac7"


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


async def _register_fake_host(
    app: FastAPI,
    db_uri: str,
) -> list[HostRemoveWorktreeFrame]:
    """Register a fake host and start a drain that captures remove frames.

    :param app: The app whose ``host_registry`` to register into.
    :param db_uri: DB URI so the host row (FK target) can be upserted.
    :returns: A list that accumulates every ``HostRemoveWorktreeFrame``
        the server sends to this host.
    """
    # Upsert the host row so the conversation's host_id FK resolves.
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
    registry = app.state.host_registry
    conn = registry.register(
        host_id=_HOST_ID,
        ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
        owner=RESERVED_USER_LOCAL,
    )
    captured: list[HostRemoveWorktreeFrame] = []

    async def _drain() -> None:
        """Capture remove-worktree frames and reply ok."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostRemoveWorktreeFrame):
                captured.append(frame)
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result({"status": "ok", "error": None})

    task = asyncio.create_task(_drain())
    # Stash so the caller can stop the drain on teardown.
    conn._drain_task_for_test = task  # type: ignore[attr-defined]
    return captured


_WORKTREE_PATH = "/Users/alice/myrepo-worktrees/feature-login"


def _make_worktree_conversation(db_uri: str, workspace: str = _WORKTREE_PATH) -> str:
    """Create a session row that looks like a server-created worktree.

    :param db_uri: DB URI for the conversation store.
    :param workspace: Worktree path to record; defaults to the shared
        fixture path so two calls produce two sessions in one directory.
    :returns: The new conversation id.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace=workspace,
        git_branch="feature/login",
    )
    return conv.id


async def test_delete_with_flag_sends_remove_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a worktree session sends a
    host.remove_worktree frame carrying the stored path + branch.

    If no frame is captured, the delete-flow gate or the proxy call
    is broken and the user's checkbox would silently do nothing. The
    path/branch assertions prove the server uses the *stored* values
    (not request input), which is the multi-user-safe contract.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200

    # Exactly one remove frame, with the stored worktree path/branch
    # and delete_branch=True (the box was checked).
    assert len(captured) == 1, (
        f"Expected exactly one host.remove_worktree frame, got {len(captured)}. "
        "0 means the delete-flow cleanup gate didn't fire; >1 means it fired twice."
    )
    frame = captured[0]
    assert frame.worktree_path == "/Users/alice/myrepo-worktrees/feature-login"
    assert frame.branch == "feature/login"
    assert frame.delete_branch is True


async def test_delete_without_flag_sends_no_remove_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Deleting a worktree session WITHOUT the flag leaves the worktree
    alone — no host.remove_worktree frame is sent.

    If a frame is captured here, the cleanup is happening
    unconditionally and would destroy worktrees/branches the user
    never asked to remove.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}")
    assert resp.status_code == 200
    # Default is delete_branch=false → no cleanup.
    assert captured == []


async def test_delete_shared_worktree_keeps_it_until_the_last_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Two sessions in one worktree: deleting the first leaves the directory,
    deleting the last removes it.

    A fork reusing the source's worktree, or two sessions attached to the
    same existing one, both run in the same cwd. If the first delete
    removed it, the survivor's runner would be left on a deleted
    directory and stop responding.
    """
    captured = await _register_fake_host(app, db_uri)
    first = _make_worktree_conversation(db_uri)
    second = _make_worktree_conversation(db_uri)
    assert first != second

    resp = await client.delete(f"/v1/sessions/{first}?delete_branch=true")
    assert resp.status_code == 200
    assert captured == [], (
        "worktree removed while another live session still runs there — "
        "that session's runner is now on a deleted directory"
    )

    resp = await client.delete(f"/v1/sessions/{second}?delete_branch=true")
    assert resp.status_code == 200
    assert len(captured) == 1, "the last session out must remove the worktree"
    assert captured[0].worktree_path == _WORKTREE_PATH
    assert captured[0].delete_branch is True


async def test_delete_removes_worktree_shared_only_with_archived_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    An archived session in the same worktree does not keep it alive.

    Archived sessions run nothing, so the directory can go. Counting them
    would mean a worktree shared by two forks is never cleaned up once
    either one is archived — the cleanup would silently never happen.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    archived = _make_worktree_conversation(db_uri)
    conv_store.update_conversation(archived, archived=True)
    live = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{live}?delete_branch=true")
    assert resp.status_code == 200
    assert len(captured) == 1, "only an archived session shares this path — remove it"
    assert captured[0].worktree_path == _WORKTREE_PATH


def test_has_other_live_session_in_workspace(db_uri: str) -> None:
    """The store gate itself: other live sessions count, self and archived
    sessions don't, and a different path never does."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    mine = _make_worktree_conversation(db_uri)

    # Alone in the directory.
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )

    # A fork reusing the directory — visible from either side.
    theirs = _make_worktree_conversation(db_uri)
    for viewer in (mine, theirs):
        assert conv_store.has_other_live_session_in_workspace(
            host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=viewer
        )

    # Archiving the other one frees the directory.
    conv_store.update_conversation(theirs, archived=True)
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )

    # A different path, and a different host, never count.
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace="/Users/alice/elsewhere", exclude_conversation_id=mine
    )
    assert not conv_store.has_other_live_session_in_workspace(
        host_id="b" * 32, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )


def test_has_other_live_session_answers_in_use_past_the_scan_bound(db_uri: str) -> None:
    """More sharers than the scan bound answers "in use" without checking
    archived state — the safe direction, since a wrong "free" deletes a
    directory a running session is sitting in."""
    from omnigent.stores.conversation_store import sqlalchemy_store

    conv_store = SqlAlchemyConversationStore(db_uri)
    mine = _make_worktree_conversation(db_uri)
    others = [
        _make_worktree_conversation(db_uri)
        for _ in range(sqlalchemy_store._WORKSPACE_SHARER_SCAN_LIMIT + 1)
    ]
    # Archived to the last one: the bound short-circuits before archived state
    # is consulted, so the answer is still "in use".
    for cid in others:
        conv_store.update_conversation(cid, archived=True)

    assert conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )


def test_shared_worktree_check_stays_cheap(db_uri: str) -> None:
    """
    The gate's cost, which the delete path is sensitive to: one query when
    nothing else is in the directory, and a second only when it really is
    shared.

    A regression here is invisible behaviourally — the delete still returns
    the right answer, just slower on every session delete.
    """
    from sqlalchemy import event

    conv_store = SqlAlchemyConversationStore(db_uri)
    mine = _make_worktree_conversation(db_uri)

    statements: list[str] = []
    for engine in {conv_store._engine, conv_store._conv_engine}:
        event.listen(
            engine,
            "before_cursor_execute",
            # PRAGMAs are per-connection setup, not work this gate asked for.
            lambda conn, cur, stmt, params, ctx, many: (
                statements.append(stmt) if not stmt.startswith("PRAGMA") else None
            ),
        )

    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )
    assert len(statements) == 1, (
        f"expected a single query when the directory is unshared, got {len(statements)}: "
        f"{statements}. The archived filter must not open the second database "
        "on the common path."
    )

    _make_worktree_conversation(db_uri)
    statements.clear()
    assert conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )
    assert len(statements) == 2, (
        f"a shared directory should cost the candidate query plus the archived "
        f"filter, got {len(statements)}: {statements}"
    )

    # Past the bound the answer is already settled, so the archived filter is
    # skipped and its IN list can never grow with the directory.
    from omnigent.stores.conversation_store import sqlalchemy_store

    for _ in range(sqlalchemy_store._WORKSPACE_SHARER_SCAN_LIMIT):
        _make_worktree_conversation(db_uri)
    statements.clear()
    assert conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )
    assert len(statements) == 1, (
        "past the scan bound the answer is already known, so the archived filter "
        f"must not run: {statements}"
    )


async def test_delete_non_worktree_session_ignores_flag(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a session with no worktree
    (``git_branch`` NULL) is a no-op — no remove frame.

    The gate keys off ``git_branch IS NOT NULL``; without that check a
    plain session delete would try to remove a worktree that doesn't
    exist.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    # Plain session: no host, no workspace, no git_branch.
    conv = conv_store.create_conversation(agent_id=None)

    resp = await client.delete(f"/v1/sessions/{conv.id}?delete_branch=true")
    assert resp.status_code == 200
    assert captured == []


def _upsert_host_row(db_uri: str) -> None:
    """Insert the host row without registering a live tunnel.

    :param db_uri: DB URI so the conversation's host_id FK resolves.
    """
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)


class _OfflineRunnerRouter:
    """Runner router that reports every bound session's runner as offline."""

    def client_for_session_resources(self, session_id: str, **kwargs: object) -> object:
        del session_id, kwargs
        raise OmnigentError(
            "runner 'runner_token_offline' is offline",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _assert_worktree_offline_conflict(resp: httpx.Response) -> None:
    """Assert the Option B 409 body for offline worktree cleanup.

    :param resp: DELETE response that should refuse the cleanup.
    """
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "conflict"
    assert "runner offline" in error["message"]
    assert "delete_branch=false" in error["message"]


def _assert_session_still_exists(db_uri: str, conv_id: str) -> None:
    """The conversation row must still be in the store.

    These fixture sessions have no agent binding, so ``GET /v1/sessions/{id}``
    500s on snapshot build. The store read is the existence check.

    :param db_uri: DB URI for the conversation store.
    :param conv_id: Session id that must still exist.
    """
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(conv_id)
    assert conv is not None, (
        f"session {conv_id} was deleted; it must remain after a refused worktree cleanup"
    )


async def test_delete_with_flag_when_host_offline_returns_conflict(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a worktree session whose host is not
    connected must 409 with an actionable message — not 404, and not
    a silent skip that leaves the caller thinking the session is gone.

    The git worktree lives on the host; an offline host cannot run
    ``git worktree remove``. The session must remain so the user can
    retry with ``delete_branch=false``.
    """
    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)


async def test_refused_delete_keeps_session_files(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A 409-refused delete must be non-destructive end to end: the
    session's files must survive alongside the row, so a later retry
    (runner back online, or without the flag) deletes a fully intact
    session rather than one whose files were already destroyed.
    """
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    stored = file_store.create("notes.txt", 4, "text/plain", session_id=conv_id)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)
    assert file_store.get(stored.id, session_id=conv_id) is not None, (
        "the refused delete must not have destroyed the session's files"
    )


async def test_delete_without_flag_when_host_offline_still_deletes(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Without ``delete_branch``, an offline host must not block delete."""
    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404


async def test_delete_with_flag_when_runner_offline_and_host_offline_returns_conflict(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Worktree session, runner unreachable, delete_branch set. Must not
    map to 404 (session-not-found); the session exists and
    the user owns it — cleanup cannot proceed because the host/runner
    tunnel is down.
    """
    from omnigent.runtime import _globals, set_runner_router

    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    prior = _globals._runner_router
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    try:
        resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    finally:
        set_runner_router(prior)

    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)


async def test_delete_with_flag_when_runner_offline_but_host_online_cleans_up(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Git cleanup rides the host tunnel, not the runner. A dead runner on
    a still-connected host must still send ``host.remove_worktree`` and
    delete the session — failing this would block cleanup that can proceed.
    """
    from omnigent.runtime import _globals, set_runner_router

    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    prior = _globals._runner_router
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    try:
        resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    finally:
        set_runner_router(prior)

    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0].delete_branch is True

    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404


async def test_delete_shared_worktree_when_host_offline_still_deletes_non_last(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    An offline host must not 409 deleting a session that shares its
    worktree — that delete would not have removed the directory.
    """
    _upsert_host_row(db_uri)
    first = _make_worktree_conversation(db_uri)
    second = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{first}?delete_branch=true")
    assert resp.status_code == 200, resp.text

    # Last remaining session still needs the host to clean up.
    resp = await client.delete(f"/v1/sessions/{second}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, second)


async def test_delete_with_flag_when_host_drops_during_remove_returns_conflict(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that drops mid-remove is the same as offline: 409, session stays."""
    from omnigent.server.routes._host_worktree import WorktreeHostUnavailableError

    await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    async def _unavailable(**_kwargs: object) -> None:
        raise WorktreeHostUnavailableError("host connection lost during worktree removal")

    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.remove_worktree_on_host",
        _unavailable,
    )

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)


async def test_delete_with_flag_still_succeeds_on_host_git_failure(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable host that reports a git error must not block the delete.

    Unavailability is the caller's problem (retry without the flag).
    A git failure after we reached the host is still best-effort.
    """
    from omnigent.server.routes._host_worktree import WorktreeProxyError

    await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    async def _git_failed(**_kwargs: object) -> None:
        raise WorktreeProxyError("worktree removal failed: not a git repo")

    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.remove_worktree_on_host",
        _git_failed,
    )

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200, resp.text
    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404
