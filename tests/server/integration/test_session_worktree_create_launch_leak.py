"""Regression: a worktree this inline ``POST /v1/sessions`` create made is
removed and its binding cleared on launch failure, while a user's existing
worktree is kept for the lenient first-message relaunch."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "7f0c2a9d8e4b41f6a1c3d5e7b9a0c2d4"
_SOURCE_REPO = "/Users/alice/myrepo"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App wired WITH ``host_store`` so the inline create path attempts a
    host launch (the shared ``app`` fixture passes ``host_store=None``)."""
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
        """No-op send; frames flow through the outbound queue."""


@dataclass
class _HostCapture:
    """Control frames a fake host received during one request, plus the
    worktrees it reports so a cleanup-time ``host.list_worktrees`` has
    something to list."""

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    launch: list[HostLaunchRunnerFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)
    worktrees: list[dict[str, Any]] = field(default_factory=list)


RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_worktree_launch_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory registering a fake host that answers and captures
    stat/create/launch/list/remove frames. ``launch_status`` models the launch
    verdict; ``workspace_subdir`` relocates the picked subdir into the worktree."""
    conns: list[HostConnection] = []

    def _register(
        *, launch_status: str = "launched", workspace_subdir: str | None = None
    ) -> _HostCapture:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type]
            hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
            owner=RESERVED_USER_LOCAL,
        )
        cap = _HostCapture()

        async def _drain() -> None:
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
                    branch_dir = frame.branch_name.replace("/", "-")
                    worktree_path = f"{_SOURCE_REPO}-worktrees/{branch_dir}"
                    cap.worktrees.append(
                        {"path": worktree_path, "branch": frame.branch_name, "is_main": False}
                    )
                    workspace = f"{worktree_path}/{workspace_subdir}" if workspace_subdir else None
                    fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "worktree_path": worktree_path,
                                "workspace": workspace,
                                "branch": frame.branch_name,
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostLaunchRunnerFrame):
                    cap.launch.append(frame)
                    fut = conn.pending_launches.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        launched = launch_status == "launched"
                        fut.set_result(
                            {
                                "status": launch_status,
                                "runner_id": "runner_from_host" if launched else None,
                                "error": None if launched else "boom",
                            }
                        )
                elif isinstance(frame, HostListWorktreesFrame):
                    fut = conn.pending_list_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result({"status": "ok", "worktrees": list(cap.worktrees)})
                elif isinstance(frame, HostRemoveWorktreeFrame):
                    cap.remove.append(frame)
                    fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result({"status": "ok", "error": None})

        conn._drain_task_for_test = asyncio.create_task(_drain())  # type: ignore[attr-defined]
        conns.append(conn)
        return cap

    yield _register

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
    """POST a JSON session-create with a ``git`` block on the target host."""
    return await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": git,
        },
    )


async def test_inline_create_launch_failure_cleans_up_worktree(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A launch failure after the worktree is created sends one
    ``host.remove_worktree`` and clears the host binding, matching the
    dedicated bind endpoint; otherwise the worktree leaks until storage fills."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-leak-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/leak", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None

    assert len(cap.create) == 1, f"expected one create_worktree frame, got {len(cap.create)}"
    assert len(cap.launch) == 1, f"expected one launch_runner frame, got {len(cap.launch)}"

    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-leak"
    observed = {
        "create_status": resp.status_code,
        "create_frames": len(cap.create),
        "launch_frames": len(cap.launch),
        "remove_frames": len(cap.remove),
        "session_workspace": conv.workspace,
        "session_git_branch": conv.git_branch,
        "session_host_id": conv.host_id,
        "session_runner_id_set": conv.runner_id is not None,
    }
    assert len(cap.remove) == 1, (
        "ABANDONED WORKTREE (leak): the inline create path sent no host.remove_worktree "
        f"on launch failure, so {worktree_path} is left on the host. observed={observed}"
    )
    assert cap.remove[0].worktree_path == worktree_path
    assert cap.remove[0].delete_branch is True

    # Binding is cleared so no later cleanup acts on a stale workspace/branch.
    assert conv.host_id is None, observed
    assert conv.workspace is None, observed
    assert conv.git_branch is None, observed
    assert conv.runner_id is None, observed
    assert WORKTREE_ROOT_LABEL_KEY not in conv.labels, observed

    # The create response reports the cleared binding, not the removed worktree.
    body = resp.json()
    assert body["host_id"] is None and body["runner_id"] is None, body
    assert body["workspace"] is None and body["git_branch"] is None, body
    assert WORKTREE_ROOT_LABEL_KEY not in body["labels"], body


async def test_inline_create_launch_failure_removes_relocated_worktree_root(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A picked repo subdirectory is relocated inside the new worktree, so
    the rollback must remove the worktree root, not that subdirectory."""
    cap = register_worktree_launch_host(launch_status="failed", workspace_subdir="packages/app")
    agent = await create_test_agent(client, name="wt-subdir-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": f"{_SOURCE_REPO}/packages/app",
            "git": {"branch_name": "feature/sub", "base_branch": "main"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1 and cap.create[0].repo_path == f"{_SOURCE_REPO}/packages/app"
    assert len(cap.launch) == 1, f"expected one launch_runner frame, got {len(cap.launch)}"
    assert len(cap.remove) == 1, f"expected one remove_worktree frame, got {cap.remove}"
    assert cap.remove[0].worktree_path == f"{_SOURCE_REPO}-worktrees/feature-sub"
    assert cap.remove[0].delete_branch is True

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.host_id is None and conv.workspace is None and conv.git_branch is None
    assert WORKTREE_ROOT_LABEL_KEY not in conv.labels


async def test_inline_create_existing_worktree_launch_failure_keeps_worktree(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Binding to a PRE-EXISTING worktree then failing to launch must NOT
    remove it (the user's, not an orphan); the session stays bound so the
    first message can retry the launch."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-bind-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/mine", "existing_worktree": True}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.launch) == 1, f"expected one launch_runner frame, got {len(cap.launch)}"
    assert cap.create == [], "existing_worktree must not create a worktree"
    assert cap.remove == [], f"a user's existing worktree must never be removed: {cap.remove}"

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.host_id == _HOST_ID
    assert conv.workspace == _SOURCE_REPO
    assert conv.git_branch == "feature/mine"


async def test_delete_with_delete_branch_removes_worktree(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A successfully-started session's worktree is removed on opt-in
    ``DELETE ...?delete_branch=true``. Launch succeeds here, so no
    create-time cleanup fires; the delete path sends the remove frame."""
    cap = register_worktree_launch_host(launch_status="launched")
    agent = await create_test_agent(client, name="wt-del-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/keep", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-keep"

    # Successful launch leaves the worktree in place; nothing removed yet.
    assert cap.remove == [], f"successful create unexpectedly removed a worktree: {cap.remove}"

    del_resp = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert del_resp.status_code in (200, 204), del_resp.text
    assert len(cap.remove) == 1, "delete_branch=true should send host.remove_worktree"
    assert cap.remove[0].worktree_path == worktree_path
    assert cap.remove[0].delete_branch is True
