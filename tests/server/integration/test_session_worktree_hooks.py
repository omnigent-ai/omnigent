"""
Integration tests for user-defined worktree lifecycle commands.

Drives the full app with a fake host that answers the worktree control frames,
including ``host.run_worktree_hook``. Covers both hooks end to end: the
post-create setup command (which paths fire it, what it leaves in the
transcript, and that a failure is non-fatal) and the pre-delete teardown
command (which must run before removal and never block the delete).

The setup command is dispatched detached, so the assertions poll the session
snapshot for the settled ``omnigent.worktree_setup`` label rather than assuming
it landed by the time the create response returned.
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
    HostRunWorktreeHookFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "4c9864c45b72c1abf46b12247e51dbe0"
_SOURCE_REPO = "/Users/alice/myrepo"
_SETUP_LABEL = "omnigent.worktree_setup"


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
        the teardown command ran BEFORE the worktree was removed.
    """

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)
    hooks: list[HostRunWorktreeHookFrame] = field(default_factory=list)
    order: list[str] = field(default_factory=list)


# register(*, hook_status=, hook_exit_code=, hook_timed_out=, hook_output=,
#          hook_error=) -> _HostCapture
RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_hook_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory registering a fake host that answers hook frames.

    The drain answers ``host.stat`` (so workspace validation passes),
    ``host.create_worktree``, ``host.remove_worktree``, and
    ``host.run_worktree_hook`` with a caller-chosen outcome.

    :param app: App whose ``host_registry`` to register into.
    :param db_uri: DB URI so the ``host_id`` FK target row exists.
    :returns: Async iterator yielding a ``register`` factory whose kwargs
        shape the hook reply: ``hook_status`` (``"ok"`` = the hook ran,
        ``"failed"`` = the host could not start it), ``hook_exit_code``,
        ``hook_timed_out``, ``hook_output``, ``hook_error``, and
        ``hook_gate`` (an event the drain waits on before replying, so a
        test can hold the hook mid-run). Returns a ``_HostCapture``
        accumulating every frame the host received.
    """
    conns: list[HostConnection] = []

    def _register(
        *,
        hook_status: str = "ok",
        hook_exit_code: int | None = 0,
        hook_timed_out: bool = False,
        hook_output: str = "",
        hook_error: str | None = None,
        hook_gate: asyncio.Event | None = None,
    ) -> _HostCapture:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "hook-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="hook-host"),
            owner=RESERVED_USER_LOCAL,
        )
        cap = _HostCapture()

        async def _drain() -> None:
            """Answer stat / worktree / hook frames; capture them in order."""
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
                    fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        dirname = frame.branch_name.replace("/", "-")
                        fut.set_result(
                            {
                                "status": "ok",
                                "worktree_path": f"{frame.repo_path}-worktrees/{dirname}",
                                "branch": frame.branch_name,
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostRunWorktreeHookFrame):
                    cap.hooks.append(frame)
                    cap.order.append(f"hook:{frame.hook}")
                    fut = conn.pending_worktree_hooks.pop(frame.request_id, None)
                    if hook_gate is not None:
                        # Hold the reply so a test can observe the window in
                        # which the first turn must not dispatch.
                        await hook_gate.wait()
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": hook_status,
                                "exit_code": hook_exit_code,
                                "timed_out": hook_timed_out,
                                "output_tail": hook_output,
                                "error": hook_error,
                            }
                        )
                elif isinstance(frame, HostRemoveWorktreeFrame):
                    cap.remove.append(frame)
                    cap.order.append("remove")
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


async def _create_project(
    client: httpx.AsyncClient,
    name: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Create a project holding worktree hook config.

    :param client: The test HTTP client.
    :param name: Project name (also the ``omni_project`` label value).
    :param config: The project's ``config`` object.
    :returns: The created project row.
    """
    resp = await client.post("/v1/projects", json={"name": name, "config": config})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create_session_in_project(
    client: httpx.AsyncClient,
    agent_id: str,
    project_name: str | None,
    git: dict[str, Any] | None,
) -> httpx.Response:
    """POST a session create, born filed into ``project_name``.

    The web composer files a new session by stamping the legacy
    ``omni_project`` label at create time (the first-class ``project_id``
    is written by a follow-up PATCH), so that's what the server resolves
    the hook config through here.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind.
    :param project_name: Project to file into, or ``None`` for unfiled.
    :param git: The ``git`` block, or ``None`` for no worktree.
    :returns: The raw create response.
    """
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "host_id": _HOST_ID,
        "workspace": _SOURCE_REPO,
    }
    if git is not None:
        body["git"] = git
    if project_name is not None:
        body["labels"] = {"omni_project": project_name}
    return await client.post("/v1/sessions", json=body)


async def _await_setup_settled(
    client: httpx.AsyncClient,
    session_id: str,
    timeout_s: float = 5.0,
) -> str:
    """Poll a session snapshot until its setup label settles.

    :param client: The test HTTP client.
    :param session_id: Session to poll.
    :param timeout_s: How long to wait before failing the test.
    :returns: The settled label value, ``"done"`` or ``"failed"``.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        resp = await client.get(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200, resp.text
        state = (resp.json().get("labels") or {}).get(_SETUP_LABEL)
        if state in ("done", "failed"):
            return state
        await asyncio.sleep(0.02)
    pytest.fail(f"worktree setup for {session_id} never settled")


async def test_post_create_hook_runs_in_the_new_worktree(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A project's setup command runs in the created worktree, with context.

    Proves the whole chain: project config → server resolution → the
    ``host.run_worktree_hook`` frame carrying the command, the created
    worktree path, the branch, and the clamped timeout.
    """
    cap = register_hook_host(hook_output="bun install done\n")
    await _create_project(
        client,
        "Hooked",
        {
            "worktree_post_create_command": "bun install",
            "worktree_hook_timeout_seconds": 600,
        },
    )
    agent = await create_test_agent(client, name="hook-create-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Hooked", {"branch_name": "feature/login"}
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert await _await_setup_settled(client, session_id) == "done"

    assert len(cap.hooks) == 1, f"expected one hook frame, got {len(cap.hooks)}"
    hook = cap.hooks[0]
    assert hook.hook == "post_create"
    assert hook.command == "bun install"
    assert hook.worktree_path == f"{_SOURCE_REPO}-worktrees/feature-login"
    assert hook.branch == "feature/login"
    assert hook.timeout_seconds == 600.0
    # The worktree must exist before the hook runs in it.
    assert cap.order.index("create") < cap.order.index("hook:post_create")


async def test_multi_line_script_reaches_the_host_verbatim(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A multi-line script survives config → server → frame unchanged.

    Every layer in between (JSON config column, the hook resolver, the
    tunnel frame) has to carry the newlines; a single ``.strip()`` or
    ``.splitlines()[0]`` anywhere would silently run a different program.
    """
    cap = register_hook_host()
    script = "#!/usr/bin/env bash\nset -euo pipefail\n\nbun install\n  cp ../../.env ."
    await _create_project(client, "Hooked-script", {"worktree_post_create_command": script})
    agent = await create_test_agent(client, name="hook-script-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Hooked-script", {"branch_name": "feature/script"}
    )
    session_id = resp.json()["id"]
    assert await _await_setup_settled(client, session_id) == "done"

    assert len(cap.hooks) == 1
    assert cap.hooks[0].command == script
    # The transcript records the whole script, not just its first line.
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    recorded = next(i for i in items if i["type"] == "terminal_command")["input"]
    assert recorded == script


async def test_post_create_hook_success_records_command_and_output(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A successful setup command leaves a command + output pair, no error."""
    register_hook_host(hook_output="installed 42 packages\n")
    await _create_project(client, "Hooked-ok", {"worktree_post_create_command": "bun install"})
    agent = await create_test_agent(client, name="hook-ok-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Hooked-ok", {"branch_name": "wip"}
    )
    session_id = resp.json()["id"]
    assert await _await_setup_settled(client, session_id) == "done"

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    terminal = [i for i in items if i["type"] == "terminal_command"]
    assert [i["kind"] for i in terminal] == ["input", "output"]
    assert terminal[0]["input"] == "bun install"
    assert "installed 42 packages" in terminal[1]["stdout"]
    # Success leaves no error banner.
    assert not [i for i in items if i["type"] == "error"]


async def test_post_create_hook_failure_is_non_fatal_and_persisted(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A failing setup command still yields a usable session, plus a banner.

    Fail-open is the contract: the create succeeds (201), the session is
    reachable, and the failure lands as a durable ``error`` item so a
    refresh still shows the degraded workspace.
    """
    register_hook_host(hook_exit_code=1, hook_output="error: lockfile out of date\n")
    await _create_project(client, "Hooked-bad", {"worktree_post_create_command": "bun install"})
    agent = await create_test_agent(client, name="hook-bad-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Hooked-bad", {"branch_name": "wip2"}
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert await _await_setup_settled(client, session_id) == "failed"

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    errors = [i for i in items if i["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "worktree_setup_failed"
    assert "status 1" in errors[0]["message"]
    assert "setup script" in errors[0]["message"]
    # The 10 KB output tail is viewable alongside the banner.
    output = [i for i in items if i["type"] == "terminal_command"][1]
    assert "lockfile out of date" in output["stdout"]


async def test_post_create_hook_timeout_is_reported_as_timed_out(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A timed-out setup command settles ``failed`` and says it timed out."""
    register_hook_host(hook_exit_code=None, hook_timed_out=True, hook_output="installing…")
    await _create_project(client, "Hooked-slow", {"worktree_post_create_command": "sleep 999"})
    agent = await create_test_agent(client, name="hook-slow-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Hooked-slow", {"branch_name": "wip3"}
    )
    session_id = resp.json()["id"]
    assert await _await_setup_settled(client, session_id) == "failed"

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    errors = [i for i in items if i["type"] == "error"]
    assert "timed out" in errors[0]["message"]


async def test_post_create_hook_never_fires_for_bind_mode(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Binding to an existing worktree runs no setup command.

    The user prepared that worktree themselves; re-running setup in it is
    not Omnigent's call (and nothing was created here).
    """
    cap = register_hook_host()
    await _create_project(client, "Hooked-bind", {"worktree_post_create_command": "bun install"})
    agent = await create_test_agent(client, name="hook-bind-agent")

    resp = await _create_session_in_project(
        client,
        agent["id"],
        "Hooked-bind",
        {"branch_name": "already-there", "existing_worktree": True},
    )
    assert resp.status_code == 201, resp.text
    # Give a (wrongly) scheduled hook a chance to land before asserting none did.
    await asyncio.sleep(0.1)
    assert cap.hooks == []
    labels = (await client.get(f"/v1/sessions/{resp.json()['id']}")).json()["labels"] or {}
    assert _SETUP_LABEL not in labels


async def test_no_hook_configured_leaves_no_setup_state(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A project with no hook keys behaves exactly like today."""
    cap = register_hook_host()
    await _create_project(client, "Plain", {"base_branch": "main"})
    agent = await create_test_agent(client, name="hook-none-agent")

    resp = await _create_session_in_project(client, agent["id"], "Plain", {"branch_name": "wip4"})
    assert resp.status_code == 201, resp.text
    await asyncio.sleep(0.1)
    assert cap.hooks == []
    labels = (await client.get(f"/v1/sessions/{resp.json()['id']}")).json()["labels"] or {}
    assert _SETUP_LABEL not in labels


async def test_unfiled_session_runs_no_hook(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A session in no project has no hook config to inherit."""
    cap = register_hook_host()
    await _create_project(
        client, "Hooked-elsewhere", {"worktree_post_create_command": "bun install"}
    )
    agent = await create_test_agent(client, name="hook-unfiled-agent")

    resp = await _create_session_in_project(client, agent["id"], None, {"branch_name": "wip5"})
    assert resp.status_code == 201, resp.text
    await asyncio.sleep(0.1)
    assert cap.hooks == []


async def test_pre_delete_hook_runs_before_removal_and_reports_failure(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The teardown command runs before removal and never blocks the delete.

    A dev server holding the worktree open has to stop first, so ordering
    matters. A non-zero exit still deletes the session; the outcome rides
    back in the response body because there's no row left to persist it on.
    """
    cap = register_hook_host(hook_exit_code=2, hook_output="could not stop dev server\n")
    await _create_project(
        client, "Teardown", {"worktree_pre_delete_command": "./scripts/teardown.sh"}
    )
    agent = await create_test_agent(client, name="hook-delete-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Teardown", {"branch_name": "feature/teardown"}
    )
    session_id = resp.json()["id"]

    deleted = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["deleted"] is True
    assert body["pre_delete_hook"]["exit_code"] == 2
    assert "could not stop dev server" in body["pre_delete_hook"]["output_tail"]

    # Teardown ran, and it ran BEFORE the worktree was removed.
    pre_delete = [h for h in cap.hooks if h.hook == "pre_delete"]
    assert len(pre_delete) == 1
    assert pre_delete[0].command == "./scripts/teardown.sh"
    assert pre_delete[0].worktree_path == f"{_SOURCE_REPO}-worktrees/feature-teardown"
    assert cap.order.index("hook:pre_delete") < cap.order.index("remove")
    # The session is really gone despite the failed teardown.
    assert (await client.get(f"/v1/sessions/{session_id}")).status_code == 404


async def test_pre_delete_hook_skipped_without_delete_branch(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """No worktree cleanup means no teardown command.

    Deleting without ``?delete_branch=true`` leaves the worktree on disk,
    so tearing it down would destroy state the user chose to keep.
    """
    cap = register_hook_host()
    await _create_project(
        client, "Teardown-opt", {"worktree_pre_delete_command": "./scripts/teardown.sh"}
    )
    agent = await create_test_agent(client, name="hook-delete-opt-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Teardown-opt", {"branch_name": "feature/keep"}
    )
    session_id = resp.json()["id"]
    await _await_setup_settled_or_skip(client, session_id)

    deleted = await client.delete(f"/v1/sessions/{session_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["pre_delete_hook"] is None
    assert [h for h in cap.hooks if h.hook == "pre_delete"] == []


async def test_composer_create_places_the_worktree_under_the_project_root(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A project's ``worktree_root`` reaches the host on the session-create path.

    The worktree is created BEFORE the conversation row exists, so the root
    has to be resolved from the create REQUEST (its ``omni_project`` label)
    — reading it off the session row would resolve nothing and silently fall
    back to the built-in layout.
    """
    cap = register_hook_host()
    await _create_project(client, "Rooted", {"worktree_root": ".worktrees"})
    agent = await create_test_agent(client, name="hook-root-agent")

    resp = await _create_session_in_project(
        client, agent["id"], "Rooted", {"branch_name": "feature/login"}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1
    assert cap.create[0].worktree_root == ".worktrees"


async def test_composer_create_without_a_project_root_sends_none(
    register_hook_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An unfiled session leaves the host on its built-in layout."""
    cap = register_hook_host()
    agent = await create_test_agent(client, name="hook-unfiled-agent")

    resp = await _create_session_in_project(client, agent["id"], None, {"branch_name": "wip"})
    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1
    assert cap.create[0].worktree_root is None


async def _await_setup_settled_or_skip(client: httpx.AsyncClient, session_id: str) -> None:
    """Wait out a post-create hook if this project configured one.

    :param client: The test HTTP client.
    :param session_id: Session whose setup state to drain.
    """
    resp = await client.get(f"/v1/sessions/{session_id}")
    if (resp.json().get("labels") or {}).get(_SETUP_LABEL) is not None:
        await _await_setup_settled(client, session_id)
