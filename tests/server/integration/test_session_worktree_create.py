"""
Integration tests for git worktree creation on ``POST /v1/sessions``.

Drives the JSON create path with a `git` block through the full app and
a fake host that auto-replies to the worktree control frames. Verifies
that the request's branch_name + base_branch reach the host's
``host.create_worktree`` frame, and that the created worktree path and
branch are persisted on the session. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    HostRemoveWorktreeFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_wt_create"
_SOURCE_REPO = "/Users/alice/myrepo"


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
        list proves the create-rollback path fired).
    """

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)


# Factory yielded by the ``register_worktree_host`` fixture:
# register(*, create_status=, create_error=) -> _HostCapture.
RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_worktree_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory that registers a fake host with a replying drain.

    The drain answers ``host.stat`` (so workspace validation passes) and
    ``host.create_worktree`` (capturing each frame). Every drain started
    during the test is poisoned and awaited at teardown, so no background
    task leaks into the next test's event loop (mirrors the cleanup in
    ``test_host_worktree.py``).

    :param app: App whose ``host_registry`` to register into.
    :param db_uri: DB URI so the ``host_id`` FK target row exists.
    :returns: Async iterator yielding a ``register`` factory. Its
        kwargs: ``create_status`` (``"ok"`` returns a worktree path,
        ``"failed"`` simulates a host git failure such as a bad base
        ref) and ``create_error`` (the failure message). Returns a
        ``_HostCapture`` whose ``.create`` / ``.remove`` lists accumulate
        the create- and remove-worktree frames the host received.
    """
    conns: list[HostConnection] = []

    def _register(*, create_status: str = "ok", create_error: str | None = None) -> _HostCapture:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
            owner=RESERVED_USER_LOCAL,
        )
        cap = _HostCapture()

        async def _drain() -> None:
            """Answer stat + create/remove-worktree frames; capture them."""
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


async def test_create_passes_branch_and_base_branch_to_host(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The request's branch_name + base_branch reach host.create_worktree,
    and the resulting worktree path + branch are persisted on the session.

    Proves the server route threads ``git.base_branch`` through
    ``_create_session_worktree`` → ``create_worktree_on_host`` → the
    frame. If base_branch were dropped on the route, the captured
    frame's base_branch would be ``None`` and this fails.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/login", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text

    # The host received exactly one create-worktree frame carrying both
    # the new branch and the requested base ref.
    assert len(cap.create) == 1, f"expected one create_worktree frame, got {len(cap.create)}"
    frame = cap.create[0]
    assert frame.repo_path == _SOURCE_REPO
    assert frame.branch_name == "feature/login"
    assert frame.base_branch == "main"

    # The returned worktree path becomes the session workspace, and the
    # branch is persisted (drives sidebar display + delete cleanup).
    body = resp.json()
    assert body["git_branch"] == "feature/login"
    assert body["workspace"] == f"{_SOURCE_REPO}-worktrees/feature-login"


async def test_create_without_base_branch_sends_none(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
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

    assert len(cap.create) == 1
    assert cap.create[0].branch_name == "wip"
    assert cap.create[0].base_branch is None


async def test_create_with_invalid_base_branch_fails_400(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An invalid base branch fails the create with 400 INVALID_INPUT.

    The host rejects the bad base ref (``host.create_worktree`` →
    ``status: failed``); the server maps that to INVALID_INPUT (400),
    NOT 500 — it's user-correctable input — and surfaces the host's
    reason. Worktree creation fails before ``create_conversation``, so
    no session row is created (the response carries no session id).
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

    # 400 (not 500): a bad base ref is user input, not a server fault.
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    # The host's reason is surfaced verbatim so the UI can show it.
    assert "base branch does not exist" in body["error"]["message"]


async def test_create_with_existing_worktree_persists_without_creating(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
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

    # No worktree was created — the host received no create frame.
    assert len(cap.create) == 0, f"expected no create_worktree frame, got {len(cap.create)}"

    # The existing worktree's branch is persisted; the workspace is the
    # supplied directory verbatim (no worktree-path rewrite).
    body = resp.json()
    assert body["git_branch"] == "feature/existing"
    assert body["workspace"] == _SOURCE_REPO


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
    for a *pre-existing* worktree without Omnigent creating one. The
    create-rollback (``git worktree remove --force`` + ``git branch -D``)
    is gated on Omnigent having created a worktree, NOT on ``git_branch``
    being set — otherwise a persistence failure would force-remove the
    user's own worktree and delete their branch. Assert no remove frame
    is sent when ``create_conversation`` raises on this path.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-no-destroy-agent")

    # Force the persistence step to fail after the bind path has already
    # set git_branch — the exact window the rollback guards. Patch the class
    # method (the store is a thin, stateless db_uri wrapper, and the route
    # uses its own instance) so the failure hits regardless of which
    # instance the router closed over.
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

    # Critically, the user's worktree is left untouched: the create-rollback
    # did NOT fire, so no remove_worktree frame reached the host.
    assert cap.remove == [], (
        f"create-rollback force-removed the user's existing worktree: {cap.remove}"
    )


async def test_create_failure_rolls_back_omnigent_created_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create_conversation failure DOES clean up an Omnigent-made worktree.

    The counterpart to the data-loss guard: when Omnigent creates the
    worktree (the ``git`` path) and persistence then fails, the orphan
    worktree it just made must be force-removed. Proves the narrowed
    rollback guard (gated on Omnigent having created a worktree) still
    fires for the case it is meant to clean up.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-rollback-agent")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated create_conversation failure")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _boom)

    with pytest.raises(RuntimeError, match="simulated create_conversation failure"):
        await _create_git_session(client, agent["id"], {"branch_name": "feature/orphan"})

    # Omnigent created the worktree, so the rollback force-removed it: one
    # remove frame for the worktree it just made, deleting the branch too.
    assert len(cap.create) == 1, cap.create
    assert len(cap.remove) == 1, f"expected a create-rollback remove frame, got {cap.remove}"
    assert cap.remove[0].branch == "feature/orphan"
    assert cap.remove[0].delete_branch is True
