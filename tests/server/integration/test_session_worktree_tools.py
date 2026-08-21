"""
Integration tests for the agent-facing worktree routes.

``POST``/``DELETE /v1/sessions/{id}/worktrees`` back ``sys_worktree_create`` /
``sys_worktree_remove``. They exist so an agent stops running ``git worktree
add`` into a directory it invented, so these drive the full app with a fake host
and assert the two things that makes true: the worktree lands under the
project's configured root, and the project's setup / teardown scripts run around
it with their outcome returned to the caller (the agent's tool result).

Also covers the authority boundary — the repository comes from the calling
session, and a removal target must be a linked worktree of that same repo.
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
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    HostRunWorktreeHookFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "9d1864c45b72c1abf46b12247e51db77"
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
    Frames a fake host received, in arrival order per kind.

    :param create: ``host.create_worktree`` frames received.
    :param remove: ``host.remove_worktree`` frames received.
    :param hooks: ``host.run_worktree_hook`` frames received.
    :param order: Frame-kind names in arrival order, so a test can assert
        the teardown script ran BEFORE the worktree was removed.
    :param worktrees: Paths the fake host reports as linked worktrees of
        the repo, appended as it "creates" them.
    """

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)
    hooks: list[HostRunWorktreeHookFrame] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    worktrees: list[tuple[str, str]] = field(default_factory=list)


# register(*, hook_exit_code=, hook_timed_out=, hook_output=, remove_error=)
#   -> _HostCapture
RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_worktree_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory registering a fake host answering the worktree frames.

    Unlike the hook fixture next door this one also answers
    ``host.list_worktrees``, which is how the delete route decides whether
    a path is removable, and it derives the created worktree path from the
    frame's ``worktree_root`` so a test can assert the project's layout was
    honored end to end.

    :param app: App whose ``host_registry`` to register into.
    :param db_uri: DB URI so the ``host_id`` FK target row exists.
    :returns: Async iterator yielding a ``register`` factory whose kwargs
        shape the hook reply (``hook_exit_code``, ``hook_timed_out``,
        ``hook_output``) or fail a removal (``remove_error``, e.g. the
        host's refusal to delete an unmerged branch). Returns a
        ``_HostCapture`` accumulating every frame the host received.
    """
    conns: list[HostConnection] = []

    def _register(
        *,
        hook_exit_code: int | None = 0,
        hook_timed_out: bool = False,
        hook_output: str = "",
        remove_error: str | None = None,
    ) -> _HostCapture:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-tool-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(
                version="0.1.0-test", frame_protocol_version=1, name="wt-tool-host"
            ),
            owner=RESERVED_USER_LOCAL,
        )
        cap = _HostCapture()

        async def _drain() -> None:
            """Answer stat / worktree / hook / list frames; capture them in order."""
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
                    cap.order.append("create")
                    dirname = frame.branch_name.replace("/", "-")
                    # Mirror the host's own resolution closely enough to prove
                    # the configured root crossed the tunnel: relative roots
                    # hang off the repo, an unset one uses the sibling layout.
                    if frame.worktree_root is None:
                        path = f"{frame.repo_path}-worktrees/{dirname}"
                    elif frame.worktree_root.startswith("/"):
                        path = f"{frame.worktree_root}/{dirname}"
                    else:
                        path = f"{frame.repo_path}/{frame.worktree_root}/{dirname}"
                    cap.worktrees.append((path, frame.branch_name))
                    fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "worktree_path": path,
                                "branch": frame.branch_name,
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostRunWorktreeHookFrame):
                    cap.hooks.append(frame)
                    cap.order.append(f"hook:{frame.hook}")
                    fut = conn.pending_worktree_hooks.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "exit_code": hook_exit_code,
                                "timed_out": hook_timed_out,
                                "output_tail": hook_output,
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostListWorktreesFrame):
                    cap.order.append("list")
                    fut = conn.pending_list_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "worktrees": [
                                    {
                                        "path": _SOURCE_REPO,
                                        "branch": "main",
                                        "is_main": True,
                                        "detached": False,
                                    },
                                    *(
                                        {
                                            "path": path,
                                            "branch": branch,
                                            "is_main": False,
                                            "detached": False,
                                        }
                                        for path, branch in cap.worktrees
                                    ),
                                ],
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostRemoveWorktreeFrame):
                    cap.remove.append(frame)
                    cap.order.append("remove")
                    fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        if remove_error is None:
                            fut.set_result({"status": "ok", "error": None})
                        else:
                            fut.set_result({"status": "failed", "error": remove_error})

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


async def _session_in_project(
    client: httpx.AsyncClient,
    *,
    name: str,
    config: dict[str, Any] | None = None,
    bind_host: bool = True,
    git: dict[str, Any] | None = None,
) -> str:
    """Create a project (optional) plus a session filed into it.

    :param client: The test HTTP client.
    :param name: Base name for the project and agent.
    :param config: The project's ``config`` object, or ``None`` to file the
        session into no project at all.
    :param bind_host: When ``False``, create the session with no host or
        workspace (so it has no repository to branch).
    :param git: The create request's ``git`` block, so the session itself
        starts in a worktree (as an orchestrator's does).
    :returns: The created session id.
    """
    body: dict[str, Any] = {}
    if config is not None:
        resp = await client.post("/v1/projects", json={"name": name, "config": config})
        assert resp.status_code in (200, 201), resp.text
        body["labels"] = {"omni_project": name}
    agent = await create_test_agent(client, name=f"{name}-agent")
    body["agent_id"] = agent["id"]
    if bind_host:
        body["host_id"] = _HOST_ID
        body["workspace"] = _SOURCE_REPO
    if git is not None:
        body["git"] = git
    resp = await client.post("/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_agent_created_worktree_lands_under_the_project_root(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The whole point: an agent's worktree honors the project's location.

    Without this the agent picks its own directory and a repo accumulates a
    different layout per tool.
    """
    cap = register_worktree_host()
    session_id = await _session_in_project(
        client, name="Rooted", config={"worktree_root": ".worktrees"}
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/worktrees",
        json={"branch_name": "polly/task-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["worktree_path"] == f"{_SOURCE_REPO}/.worktrees/polly-task-1"
    assert body["branch"] == "polly/task-1"
    # The repo is never a parameter — it comes from the session's workspace.
    assert len(cap.create) == 1
    assert cap.create[0].repo_path == _SOURCE_REPO
    assert cap.create[0].worktree_root == ".worktrees"


async def test_unconfigured_project_keeps_the_builtin_layout(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """No ``worktree_root`` configured behaves exactly as before."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="Plain", config={})

    resp = await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "wip"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["worktree_path"] == f"{_SOURCE_REPO}-worktrees/wip"
    assert cap.create[0].worktree_root is None


async def test_worktree_forks_from_the_calling_sessions_own_branch(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A fan-out anchors on the ORCHESTRATOR's tree, not the main checkout.

    The host resolves every worktree off the MAIN work tree, so a bare
    ``git worktree add -b`` forks from the main checkout's HEAD and silently
    discards whatever the orchestrator's own session worktree is sitting on.
    """
    cap = register_worktree_host()
    session_id = await _session_in_project(
        client, name="Anchored", config={}, git={"branch_name": "polly/session"}
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "polly/task-1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_branch"] == "polly/session"
    # The create for the CHILD worktree is the second one (the first made the
    # session's own tree).
    assert cap.create[-1].base_branch == "polly/session"


async def test_explicit_base_branch_still_wins(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The default is a convenience, not a constraint."""
    cap = register_worktree_host()
    session_id = await _session_in_project(
        client, name="Overridden", config={}, git={"branch_name": "polly/session"}
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/worktrees",
        json={"branch_name": "hotfix", "base_branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_branch"] == "main"
    assert cap.create[-1].base_branch == "main"


async def test_session_with_no_branch_falls_back_to_repo_head(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A session running directly in the checkout has no branch to anchor on."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="NoBranch", config={})

    resp = await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_branch"] is None
    assert cap.create[-1].base_branch is None


async def test_setup_script_runs_and_its_result_reaches_the_caller(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The project's setup script runs in the new tree, and the AGENT sees it.

    The session-create path reports a hook only on the event stream and in
    the web transcript, which the agent never reads. Here the response IS
    the tool result, so the outcome has to be in it.
    """
    cap = register_worktree_host(hook_output="bun install v1.1.0\n")
    session_id = await _session_in_project(
        client,
        name="Setup",
        config={
            "worktree_root": ".worktrees",
            "worktree_post_create_command": "bun install",
            "worktree_hook_timeout_seconds": 600,
        },
    )

    resp = await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"})
    assert resp.status_code == 200, resp.text
    setup = resp.json()["setup"]
    assert setup == {
        "ran": True,
        "ok": True,
        "exit_code": 0,
        "timed_out": False,
        "output_tail": "bun install v1.1.0\n",
        "error": None,
    }
    assert len(cap.hooks) == 1
    hook = cap.hooks[0]
    assert hook.hook == "post_create"
    assert hook.command == "bun install"
    assert hook.worktree_path == f"{_SOURCE_REPO}/.worktrees/task"
    assert hook.branch == "task"
    assert hook.timeout_seconds == 600.0
    # The tree has to exist before the script runs in it.
    assert cap.order.index("create") < cap.order.index("hook:post_create")


async def test_failing_setup_script_is_reported_but_the_worktree_survives(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Fail-open: the caller is told, and decides — the tree is not destroyed."""
    register_worktree_host(hook_exit_code=1, hook_output="error: lockfile conflict\n")
    session_id = await _session_in_project(
        client,
        name="BadSetup",
        config={"worktree_post_create_command": "bun install"},
    )

    resp = await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["worktree_path"]
    assert body["setup"]["ok"] is False
    assert body["setup"]["exit_code"] == 1
    assert "lockfile conflict" in body["setup"]["output_tail"]


async def test_no_setup_script_configured_reports_null(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """``setup: null`` distinguishes "no script" from "script succeeded"."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="NoSetup", config={})

    resp = await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["setup"] is None
    assert cap.hooks == []


async def test_teardown_script_runs_before_the_worktree_is_removed(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The teardown script must see the tree it is tearing down."""
    cap = register_worktree_host(hook_output="stopped\n")
    session_id = await _session_in_project(
        client,
        name="Teardown",
        config={
            "worktree_root": ".worktrees",
            "worktree_pre_delete_command": "docker compose down",
        },
    )
    created = await client.post(
        f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"}
    )
    path = created.json()["worktree_path"]

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        json={"worktree_path": path},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["worktree_path"] == path
    assert body["teardown"]["ok"] is True
    assert body["teardown"]["output_tail"] == "stopped\n"
    assert cap.order.index("hook:pre_delete") < cap.order.index("remove")
    # The branch survives unless asked for, so unpushed work is recoverable.
    assert cap.remove[0].delete_branch is False
    assert cap.remove[0].branch == "task"


async def test_delete_branch_is_opt_in(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """``delete_branch`` is forwarded when the caller asks for it."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="DropBranch", config={})
    created = await client.post(
        f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"}
    )

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        json={
            "worktree_path": created.json()["worktree_path"],
            "delete_branch": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert cap.remove[0].delete_branch is True


async def test_branch_deletion_is_gated_on_the_callers_own_branch(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An agent may only delete a branch whose work reached ITS branch.

    Otherwise a cleanup step that runs before integration destroys the very
    work the fan-out produced. The host does the reachability check; the
    server's job is to name the ref it must be reachable from.
    """
    cap = register_worktree_host()
    session_id = await _session_in_project(
        client, name="Gated", config={}, git={"branch_name": "polly/session"}
    )
    created = await client.post(
        f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "polly/task-1"}
    )

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        json={"worktree_path": created.json()["worktree_path"], "delete_branch": True},
    )
    assert resp.status_code == 200, resp.text
    assert cap.remove[-1].require_merged_into == "polly/session"


async def test_a_host_refusal_to_delete_an_unmerged_branch_reaches_the_caller(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The agent gets git's own reason, so it knows to integrate first."""
    cap = register_worktree_host(remove_error="refusing to delete branch 'polly/task-1'")
    session_id = await _session_in_project(
        client, name="Unmerged", config={}, git={"branch_name": "polly/session"}
    )
    created = await client.post(
        f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "polly/task-1"}
    )

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        json={"worktree_path": created.json()["worktree_path"], "delete_branch": True},
    )
    assert resp.status_code == 400, resp.text
    assert "refusing to delete branch" in resp.text
    assert cap.remove[-1].delete_branch is True


async def test_removing_a_path_outside_the_session_repo_is_refused(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A path the repo does not list as a worktree is not removable."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="Bounded", config={})

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        json={"worktree_path": "/Users/alice/someone-elses-repo"},
    )
    assert resp.status_code == 400, resp.text
    assert "not a removable worktree" in resp.text
    assert cap.remove == []


async def test_removing_the_main_checkout_is_refused(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The main work tree is listed but is not a removable worktree.

    It is also the session's own workspace here, which is refused first —
    either way no removal frame may reach the host.
    """
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="MainTree", config={})

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        json={"worktree_path": _SOURCE_REPO},
    )
    assert resp.status_code == 400, resp.text
    assert cap.remove == []


async def test_trailing_slash_and_relative_paths_resolve(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An agent's path spelling shouldn't decide whether cleanup works."""
    register_worktree_host()
    session_id = await _session_in_project(
        client, name="Spelling", config={"worktree_root": ".worktrees"}
    )
    await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"})

    resp = await client.request(
        "DELETE",
        f"/v1/sessions/{session_id}/worktrees",
        # Relative to the session's own workspace, with a trailing slash.
        json={"worktree_path": ".worktrees/task/"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["worktree_path"] == f"{_SOURCE_REPO}/.worktrees/task"


async def test_invalid_branch_name_is_rejected_before_the_host(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Ref-format validation happens server-side, so no bad argv is built."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="BadBranch", config={})

    resp = await client.post(
        f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "bad branch"}
    )
    assert resp.status_code == 400, resp.text
    assert cap.create == []


async def test_session_without_a_workspace_cannot_create_a_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """There is no repository to branch, and none may be supplied."""
    cap = register_worktree_host()
    session_id = await _session_in_project(client, name="Unbound", config={}, bind_host=False)

    resp = await client.post(f"/v1/sessions/{session_id}/worktrees", json={"branch_name": "task"})
    assert resp.status_code == 409, resp.text
    assert cap.create == []


async def test_unknown_session_is_not_found(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A bogus session id cannot borrow another session's repository."""
    register_worktree_host()
    resp = await client.post(
        "/v1/sessions/conv_does_not_exist/worktrees", json={"branch_name": "task"}
    )
    assert resp.status_code == 404, resp.text
