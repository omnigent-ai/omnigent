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
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.stores.host_store import HostStore
from tests.unit.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_wt_create"
_SOURCE_REPO = "/Users/alice/myrepo"


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


# Factory yielded by the ``register_worktree_host`` fixture:
# register(*, create_status=, create_error=) -> captured create frames.
RegisterHost = Callable[..., list[HostCreateWorktreeFrame]]


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
        ref) and ``create_error`` (the failure message). Returns the
        list that accumulates the create-worktree frames.
    """
    conns: list[HostConnection] = []

    def _register(
        *, create_status: str = "ok", create_error: str | None = None
    ) -> list[HostCreateWorktreeFrame]:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
            owner=RESERVED_USER_LOCAL,
        )
        captured: list[HostCreateWorktreeFrame] = []

        async def _drain() -> None:
            """Answer stat + create-worktree frames; capture the latter."""
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
                    captured.append(frame)
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

        conn._drain_task_for_test = asyncio.create_task(_drain())  # type: ignore[attr-defined]
        conns.append(conn)
        return captured

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
    captured = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/login", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text

    # The host received exactly one create-worktree frame carrying both
    # the new branch and the requested base ref.
    assert len(captured) == 1, f"expected one create_worktree frame, got {len(captured)}"
    frame = captured[0]
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
    captured = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent-2")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "wip"})
    assert resp.status_code == 201, resp.text

    assert len(captured) == 1
    assert captured[0].branch_name == "wip"
    assert captured[0].base_branch is None


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
    # The failed create returned an error, not a session.
    assert "id" not in body
