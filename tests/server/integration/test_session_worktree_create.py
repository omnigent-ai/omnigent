"""
Integration tests for git worktree creation on ``POST /v1/sessions``.

Drives the JSON create path with a `git` block through the full app and
a fake host that auto-replies to the worktree control frames. The
create returns as soon as the session row exists — worktree creation
and the runner launch run in a background task — so these tests verify
the immediate 201 contract (source-repo workspace, no branch yet), that
the request's branch_name + base_branch reach the host's
``host.create_worktree`` frame, and that the worktree path and branch
land on the session row once the background launch settles. See
designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostRemoveWorktreeFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "2b8753b34a61b09af35a01136d40fadf"
_SOURCE_REPO = "/Users/alice/myrepo"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path) -> FastAPI:
    """App wired WITH ``host_store`` so the host-launch branch of
    ``POST /v1/sessions`` — which owns background worktree creation —
    is active (the shared conftest app passes ``host_store=None``).

    :param runtime_init: Initializes the runtime + mock LLM.
    :param db_uri: SQLite database URI.
    :param tmp_path: Pytest temp dir for artifacts and cache.
    :returns: A configured FastAPI app with the launch branch active.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


@dataclass
class _HostCapture:
    """
    Frames a fake host received during one ``POST /v1/sessions`` create.

    :param create: ``host.create_worktree`` frames received.
    :param remove: ``host.remove_worktree`` frames received (a non-empty
        list proves the orphan-cleanup path fired).
    :param launch: ``host.launch_runner`` frames received.
    """

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)
    launch: list[HostLaunchRunnerFrame] = field(default_factory=list)


# Factory yielded by the ``register_worktree_host`` fixture:
# register(*, create_status=, create_error=, hold_create=) -> _HostCapture.
RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_worktree_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory that registers a fake host with a replying drain.

    The drain answers ``host.stat`` (so workspace validation passes),
    ``host.create_worktree`` (capturing each frame), and
    ``host.launch_runner`` (so the background launch settles promptly).
    Every drain started during the test is poisoned and awaited at
    teardown, so no background task leaks into the next test's event
    loop (mirrors the cleanup in ``test_host_worktree.py``).

    :param app: App whose ``host_registry`` to register into.
    :param db_uri: DB URI so the ``host_id`` FK target row exists.
    :returns: Async iterator yielding a ``register`` factory. Its
        kwargs: ``create_status`` (``"ok"`` returns a worktree path,
        ``"failed"`` simulates a host git failure such as a bad base
        ref), ``create_error`` (the failure message), and
        ``hold_create`` (an event the drain waits on before answering
        a create-worktree frame — lets a test act while the worktree
        is still "being created"). Returns a ``_HostCapture`` whose
        lists accumulate the frames the host received.
    """
    conns: list[HostConnection] = []

    def _register(
        *,
        create_status: str = "ok",
        create_error: str | None = None,
        hold_create: asyncio.Event | None = None,
    ) -> _HostCapture:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
            owner=RESERVED_USER_LOCAL,
        )
        cap = _HostCapture()

        async def _drain() -> None:
            """Answer stat + worktree + launch frames; capture them."""
            while True:
                frame_text = await conn.outbound_queue.get()
                if frame_text is None:
                    return
                frame = decode_host_frame(frame_text)
                if isinstance(frame, HostStatFrame):
                    fut = conn.pending_stats.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "exists": True,
                                "type": "directory",
                                "canonical_path": frame.path,
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostCreateWorktreeFrame):
                    cap.create.append(frame)
                    if hold_create is not None:
                        await hold_create.wait()
                    fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        if create_status == "ok":
                            dirname = frame.branch_name.replace("/", "-")
                            fut.set_result(
                                {
                                    "status": "ok",
                                    "worktree_path": f"{frame.repo_path}-worktrees/{dirname}",
                                    "branch": frame.branch_name,
                                    "error": None,
                                }
                            )
                        else:
                            fut.set_result(
                                {
                                    "status": "failed",
                                    "worktree_path": None,
                                    "branch": None,
                                    "error": create_error,
                                }
                            )
                elif isinstance(frame, HostRemoveWorktreeFrame):
                    cap.remove.append(frame)
                    fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result({"status": "ok", "error": None})
                elif isinstance(frame, HostLaunchRunnerFrame):
                    cap.launch.append(frame)
                    fut = conn.pending_launches.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result({"status": "launched", "runner_id": "runner_from_host"})

        conn._drain_task_for_test = asyncio.create_task(_drain())  # type: ignore[attr-defined]
        conns.append(conn)
        return cap

    yield _register

    # Poison each queue so the drain returns, then await/cancel it.
    for conn in conns:
        conn.outbound_queue.put_nowait(None)
        task = conn._drain_task_for_test  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        if not task.done():
            task.cancel()


async def _create_git_session(
    client: httpx.AsyncClient,
    agent_id: str,
    git: dict[str, Any],
) -> httpx.Response:
    """POST a JSON session-create with a ``git`` block.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind.
    :param git: The ``git`` block, e.g.
        ``{"branch_name": "feature/x", "base_branch": "main"}``.
    :returns: The raw create response.
    """
    return await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": git,
        },
    )


async def _await_launch_settled(
    app: FastAPI,
    session_id: str,
    *,
    timeout_s: float = 5.0,
) -> None:
    """Wait until the session's background create-launch has settled.

    Success pops the tracker entry; failure retains a settled entry —
    both count as settled here.

    :param app: App whose ``managed_launches`` tracker to poll.
    :param session_id: The created session's id.
    :param timeout_s: Maximum seconds to wait.
    :raises AssertionError: If the launch hasn't settled in time.
    """
    tracker = app.state.managed_launches
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entry = tracker.get(session_id)
        if entry is None or entry.settled.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"background launch for session {session_id!r} never settled")


async def test_create_passes_branch_and_base_branch_to_host(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The request's branch_name + base_branch reach host.create_worktree,
    and the resulting worktree path + branch are persisted on the session.

    The 201 itself returns the pre-worktree row (source repo, no branch)
    — worktree creation happens in the background launch task so the web
    UI can navigate immediately. Once the launch settles, the captured
    ``host.create_worktree`` frame carries both the new branch and the
    requested base ref, and the session row is re-pointed at the created
    worktree. If base_branch were dropped on the route, the captured
    frame's base_branch would be ``None`` and this fails.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/login", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Immediate contract: the create returns before the worktree exists,
    # with the source repo as workspace and no branch recorded yet.
    assert body["workspace"] == _SOURCE_REPO
    assert body["git_branch"] is None

    await _await_launch_settled(app, body["id"])

    # The host received exactly one create-worktree frame carrying both
    # the new branch and the requested base ref.
    assert len(cap.create) == 1, f"expected one create_worktree frame, got {len(cap.create)}"
    frame = cap.create[0]
    assert frame.repo_path == _SOURCE_REPO
    assert frame.branch_name == "feature/login"
    assert frame.base_branch == "main"

    # The created worktree path becomes the session workspace, and the
    # branch is persisted (drives sidebar display + delete cleanup).
    snapshot = await client.get(f"/v1/sessions/{body['id']}")
    assert snapshot.status_code == 200, snapshot.text
    updated = snapshot.json()
    assert updated["git_branch"] == "feature/login"
    assert updated["workspace"] == f"{_SOURCE_REPO}-worktrees/feature-login"


async def test_create_without_base_branch_sends_none(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Omitting base_branch sends ``None`` to the host (branch from HEAD).

    Pairs with the test above to pin both directions: a provided base
    threads through, an omitted one stays ``None`` so the host branches
    from the source repo's current HEAD.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent-2")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "wip"})
    assert resp.status_code == 201, resp.text

    await _await_launch_settled(app, resp.json()["id"])

    assert len(cap.create) == 1
    assert cap.create[0].branch_name == "wip"
    assert cap.create[0].base_branch is None


async def test_create_with_invalid_base_branch_fails_first_message(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A host-rejected base branch surfaces on the first message, not the 201.

    The create returns 201 before the host runs git; when the background
    worktree creation then fails (``host.create_worktree`` →
    ``status: failed``), the launch tracker records the host's reason and
    a message POST reports it as 503 RUNNER_UNAVAILABLE — instead of a
    generic "no runner bound". The session row keeps the source repo as
    workspace (it is never re-pointed at a worktree that doesn't exist).
    """
    register_worktree_host(
        create_status="failed",
        create_error="base branch does not exist: nope-not-a-branch",
    )
    agent = await create_test_agent(client, name="wt-bad-base-agent")

    resp = await _create_git_session(
        client,
        agent["id"],
        {"branch_name": "feature/x", "base_branch": "nope-not-a-branch"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    await _await_launch_settled(app, session_id)

    # The failure is retained on the tracker; a message reports it.
    message_resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
    )
    assert message_resp.status_code == 503, message_resp.text
    assert "base branch does not exist" in message_resp.text

    # The row was never re-pointed at a worktree.
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["workspace"] == _SOURCE_REPO
    assert snapshot.json()["git_branch"] is None


async def test_create_with_invalid_branch_name_fails_400(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A malformed create-mode ``branch_name`` is still a synchronous 400.

    Branch-name validation is the one git check the server can run
    without the host, so it stays on the create request: the user gets
    an immediate, correctable error and no session row is created.
    """
    register_worktree_host()
    agent = await create_test_agent(client, name="wt-bad-name-agent")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "bad..branch"})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    # The failed create returned an error, not a session.
    assert "id" not in body


async def test_create_with_existing_worktree_persists_without_creating(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Starting in an existing worktree persists its branch, creates nothing.

    ``git.existing_worktree`` binds the session straight to a
    pre-existing worktree directory: no create-worktree frame is sent
    to the host, and ``branch_name`` is persisted as ``git_branch`` so
    the sidebar shows it and the opt-in delete flow can offer to remove it.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-existing-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": {"branch_name": "feature/existing", "existing_worktree": True},
        },
    )
    assert resp.status_code == 201, resp.text

    # The existing worktree's branch is persisted; the workspace is the
    # supplied directory verbatim (no worktree-path rewrite).
    body = resp.json()
    assert body["git_branch"] == "feature/existing"
    assert body["workspace"] == _SOURCE_REPO

    # Even after the background launch settles, no worktree was created —
    # the host received no create frame.
    await _await_launch_settled(app, body["id"])
    assert len(cap.create) == 0, f"expected no create_worktree frame, got {len(cap.create)}"


async def test_create_with_invalid_existing_worktree_branch_fails_400(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An invalid bind-mode ``branch_name`` fails the create with 400.

    The host never runs git for this path, so the server is the only
    gate on the name; a malformed branch is user-correctable input and
    maps to INVALID_INPUT (400), not 500.
    """
    register_worktree_host()
    agent = await create_test_agent(client, name="wt-existing-bad-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": {"branch_name": "bad..branch", "existing_worktree": True},
        },
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    # The failed create returned an error, not a session.
    assert "id" not in body


async def test_create_failure_never_removes_existing_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create_conversation failure must NOT destroy the user's worktree.

    Regression: the ``existing_worktree`` bind path sets ``git_branch``
    for a *pre-existing* worktree without Omnigent creating one. A
    persistence failure on this path must never translate into a
    ``git worktree remove --force`` of the user's own directory. Assert
    no remove frame is sent when ``create_conversation`` raises.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-no-destroy-agent")

    # Force the persistence step to fail after the bind path has already
    # validated the branch name. Patch the class method (the store is a
    # thin, stateless db_uri wrapper, and the route uses its own
    # instance) so the failure hits regardless of which instance the
    # router closed over.
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated create_conversation failure")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _boom)

    # The in-process ASGI transport re-raises unhandled server errors, so
    # the simulated failure surfaces here rather than as a 500 response.
    # Either way the create failed; what matters is the side effect below.
    with pytest.raises(RuntimeError, match="simulated create_conversation failure"):
        await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_id": _HOST_ID,
                "workspace": _SOURCE_REPO,
                "git": {"branch_name": "feature/existing", "existing_worktree": True},
            },
        )

    # Critically, the user's worktree is left untouched: no remove_worktree
    # frame reached the host.
    assert cap.remove == [], (
        f"a failed create force-removed the user's existing worktree: {cap.remove}"
    )


async def test_create_failure_creates_no_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create_conversation failure leaks no worktree.

    The session row is created BEFORE any worktree work is scheduled, so
    a persistence failure means the host is never asked to create one —
    there is no orphan to roll back. Assert no create-worktree frame was
    sent.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-rollback-agent")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated create_conversation failure")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _boom)

    with pytest.raises(RuntimeError, match="simulated create_conversation failure"):
        await _create_git_session(client, agent["id"], {"branch_name": "feature/orphan"})

    assert cap.create == [], f"a failed create should never reach the host, got {cap.create}"
    assert cap.remove == []


async def test_session_deleted_during_worktree_creation_removes_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Deleting the session mid-worktree-creation cleans up the worktree.

    The create returns while the host is still running git (this test
    gates the host's reply to hold that window open — also pinning that
    the 201 does NOT wait for the worktree). A session deleted in that
    window can't see the worktree (``git_branch`` is only recorded once
    creation succeeds), so the background task detects the deleted row
    and removes the fresh worktree — branch included — instead of
    leaking it.
    """
    hold_create = asyncio.Event()
    cap = register_worktree_host(hold_create=hold_create)
    agent = await create_test_agent(client, name="wt-delete-race-agent")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "feature/doomed"})
    # The 201 returned while the create-worktree reply is still gated —
    # the create does not block on the worktree.
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    delete_resp = await client.delete(f"/v1/sessions/{session_id}")
    assert delete_resp.status_code in (200, 204), delete_resp.text

    # Let the host "finish" the worktree; the background task then finds
    # the row gone and removes the orphan.
    hold_create.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not cap.remove:
        await asyncio.sleep(0.01)

    assert len(cap.remove) == 1, f"expected an orphan-cleanup remove frame, got {cap.remove}"
    assert cap.remove[0].worktree_path == f"{_SOURCE_REPO}-worktrees/feature-doomed"
    assert cap.remove[0].branch == "feature/doomed"
    assert cap.remove[0].delete_branch is True
