"""Regression test: ``DELETE /v1/sessions/{id}`` must reap sub-agent forwarders.

Each native sub-agent gets its own restart-forever transcript forwarder keyed by
session id in ``orchestration._AUTO_FORWARDER_TASKS``. Deleting the parent
tree-deletes the children server-side, so the runner must walk the whole spawn
family and reap each native descendant; otherwise their forwarders survive and
keep POSTing events to sessions the server already deleted (404s). The walk
spans both ownership maps so a completed child whose dispatch work was already
drained is still reaped.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner import subagent_work as sw
from omnigent.runner.native import orchestration as orch
from tests.runner.helpers import NullServerClient
from tests.runner.native_forwarder_leak_helpers import (
    FakeAppServer,
    drain_forwarder,
    register_forwarder,
)


@pytest.fixture
def app() -> FastAPI:
    return create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


async def test_delete_parent_cancels_descendant_native_forwarders(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    parent = f"conv_parent_{uuid.uuid4().hex}"
    child = f"conv_child_{uuid.uuid4().hex}"
    grand = f"conv_grand_{uuid.uuid4().hex}"

    sw.register_subagent_work(
        parent_session_id=parent,
        child_session_id=child,
        agent="worker",
        title="worker",
        wrapper_label="claude-code-native-ui",
    )
    sw.register_subagent_work(
        parent_session_id=child,
        child_session_id=grand,
        agent="leaf",
        title="leaf",
        wrapper_label="claude-code-native-ui",
    )

    parent_task = register_forwarder(parent)
    child_task = register_forwarder(child)
    grand_task = register_forwarder(grand)

    # A native descendant whose forwarder never adopted its server leaves the
    # app-server registered; reaping the descendant must close it too.
    child_app_server = FakeAppServer()
    orch._AUTO_CODEX_APP_SERVERS[child] = child_app_server  # type: ignore[assignment]

    cleaned: list[str] = []
    registry = app.state.session_resource_registry
    original_cleanup = registry.cleanup_session

    async def _recording_cleanup(session_id: str) -> None:
        cleaned.append(session_id)
        await original_cleanup(session_id)

    registry.cleanup_session = _recording_cleanup  # type: ignore[method-assign]

    try:
        resp = await client.delete(f"/v1/sessions/{parent}")
        assert resp.status_code == 200

        # The route cancels the forwarder for the id it was handed.
        assert parent_task.cancelled()
        assert parent not in orch._AUTO_FORWARDER_TASKS

        # The descendants' forwarders must be cancelled too, or they keep
        # POSTing events to sessions the tree-delete already removed.
        assert child_task.cancelled(), "child sub-agent forwarder leaked after parent delete"
        assert grand_task.cancelled(), "grandchild sub-agent forwarder leaked after parent delete"
        assert child not in orch._AUTO_FORWARDER_TASKS
        assert grand not in orch._AUTO_FORWARDER_TASKS

        # Reaping a descendant must do a real per-session cleanup, not just a
        # forwarder cancel: close its orphaned native server and release its
        # panes/env, or each native descendant leaks on every tree-delete.
        assert child_app_server.closed, "codex app-server leaked for a native descendant"
        assert child not in orch._AUTO_CODEX_APP_SERVERS
        assert child in cleaned, "native descendant did not get per-session resource cleanup"
        assert grand in cleaned, "native descendant did not get per-session resource cleanup"
    finally:
        registry.cleanup_session = original_cleanup  # type: ignore[method-assign]
        orch._AUTO_CODEX_APP_SERVERS.pop(child, None)
        await drain_forwarder(parent, parent_task)
        await drain_forwarder(child, child_task)
        await drain_forwarder(grand, grand_task)
        sw.unregister_child_session(child)
        sw.unregister_child_session(grand)
        sw.unregister_subagent_work(child_session_id=child)
        sw.unregister_subagent_work(child_session_id=grand)


async def test_delete_parent_cancels_drained_child_native_forwarder(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    parent = f"conv_parent_{uuid.uuid4().hex}"
    drained = f"conv_drained_{uuid.uuid4().hex}"

    sw.register_subagent_work(
        parent_session_id=parent,
        child_session_id=drained,
        agent="worker",
        title="worker",
        wrapper_label="claude-code-native-ui",
    )
    sw.register_child_session(
        drained,
        parent_session_id=parent,
        title="worker:auth",
        tool="worker",
        session_name="auth",
    )
    # The child finished and its result was delivered, so its dispatch work is
    # drained from the ephemeral registry while the persistent child->parent
    # map still owns it. Its native session (and forwarder) outlive the drain.
    sw.unregister_subagent_work(child_session_id=drained)
    assert sw.get_subagent_work(drained) is None
    assert sw.list_subagent_work(parent) == []

    parent_task = register_forwarder(parent)
    drained_task = register_forwarder(drained)

    cleaned: list[str] = []
    registry = app.state.session_resource_registry
    original_cleanup = registry.cleanup_session

    async def _recording_cleanup(session_id: str) -> None:
        cleaned.append(session_id)
        await original_cleanup(session_id)

    registry.cleanup_session = _recording_cleanup  # type: ignore[method-assign]

    try:
        resp = await client.delete(f"/v1/sessions/{parent}")
        assert resp.status_code == 200

        assert parent_task.cancelled()
        # The drained child is invisible to the dispatch registry, so the walk
        # must discover it through the persistent session-family map.
        assert drained_task.cancelled(), "drained child forwarder leaked after parent delete"
        assert drained not in orch._AUTO_FORWARDER_TASKS
        assert drained in cleaned, "drained child did not get per-session resource cleanup"
    finally:
        registry.cleanup_session = original_cleanup  # type: ignore[method-assign]
        await drain_forwarder(parent, parent_task)
        await drain_forwarder(drained, drained_task)
        sw.unregister_child_session(drained)


async def test_delete_parent_continues_after_descendant_reap_raises(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    parent = f"conv_parent_{uuid.uuid4().hex}"
    failing = f"conv_failing_{uuid.uuid4().hex}"
    sibling = f"conv_sibling_{uuid.uuid4().hex}"

    for child in (failing, sibling):
        sw.register_subagent_work(
            parent_session_id=parent,
            child_session_id=child,
            agent="worker",
            title="worker",
            wrapper_label="claude-code-native-ui",
        )

    parent_task = register_forwarder(parent)
    failing_task = register_forwarder(failing)
    sibling_task = register_forwarder(sibling)

    cleaned: list[str] = []
    registry = app.state.session_resource_registry
    original_cleanup = registry.cleanup_session

    async def _cleanup_raising_for_failing(session_id: str) -> None:
        cleaned.append(session_id)
        if session_id == failing:
            raise RuntimeError("descendant resource cleanup blew up")
        await original_cleanup(session_id)

    registry.cleanup_session = _cleanup_raising_for_failing  # type: ignore[method-assign]

    try:
        resp = await client.delete(f"/v1/sessions/{parent}")
        # One descendant whose reap raises must not abort the delete: the server
        # already tree-deleted the session, so a 500 here would be spurious.
        assert resp.status_code == 200

        # The sibling is still reaped and the parent's own post-loop cleanup
        # still runs, so a single failing reap can't strand the rest.
        assert sibling_task.cancelled(), "a failing sibling reap stranded the other descendant"
        assert sibling in cleaned, "a failing sibling reap skipped the other descendant's cleanup"
        assert failing_task.cancelled()
        assert parent in cleaned, "a failing descendant reap aborted the parent's own cleanup"
    finally:
        registry.cleanup_session = original_cleanup  # type: ignore[method-assign]
        await drain_forwarder(parent, parent_task)
        await drain_forwarder(failing, failing_task)
        await drain_forwarder(sibling, sibling_task)
        sw.unregister_child_session(failing)
        sw.unregister_child_session(sibling)
        sw.unregister_subagent_work(child_session_id=failing)
        sw.unregister_subagent_work(child_session_id=sibling)
