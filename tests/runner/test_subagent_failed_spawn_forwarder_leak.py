"""Regression test: a failed native sub-agent spawn must not leak the child forwarder.

Binding a native sub-agent launches its harness and registers a restart-forever
transcript forwarder (``orchestration._AUTO_FORWARDER_TASKS[child]``). When the
child's first-turn message POST fails, ``_teardown_failed_child`` DELETEs the
child on the server but relies on that delete propagating back over the reverse
tunnel to cancel the runner-local forwarder — which never arrives while the
tunnel is down, so a newly-created child's forwarder keeps POSTing to a session
the server already deleted. A continued (pre-existing) child is the opposite: it
keeps its session, so a failed send must leave its forwarder running.

The ``httpx.MockTransport`` stands in for the server with no reverse tunnel to
the runner (it records the DELETE but cannot drive the runner route), and the
pre-registered forwarder stands in for the native pane the bind would launch.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import subagent_work
from omnigent.runner.native import orchestration as orch
from omnigent.runner.tool_dispatch import execute_tool
from tests.runner.native_forwarder_leak_helpers import drain_forwarder, register_forwarder


@pytest.mark.asyncio
async def test_failed_spawn_cancels_child_forwarder() -> None:
    parent_id = "conv_parent_create"
    child_id = "conv_child_leaked"
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == f"/v1/sessions/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "agent_id": "agent_parent",
                    "root_conversation_id": parent_id,
                    "parent_session_id": None,
                },
            )
        # No pre-existing child for this (parent, agent, title): proceed to create.
        if method == "GET" and path == f"/v1/sessions/{parent_id}/child_sessions":
            return httpx.Response(200, json={"data": []})
        if method == "POST" and path == "/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "id": child_id,
                    "session_id": child_id,
                    "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                },
            )
        # THE FAULT: the child's first-turn message POST fails.
        if method == "POST" and path == f"/v1/sessions/{child_id}/events":
            return httpx.Response(500, json={"error": "boom"})
        # Reverse-tunnel-less server delete: recorded, but cannot drive the
        # runner's delete_session route that would cancel the forwarder.
        if method == "DELETE" and path.startswith("/v1/sessions/"):
            deletes.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"error": f"unmocked {method} {path}"})

    # The native pane bind would have launched the harness and registered this
    # restart-forever transcript forwarder for the child.
    forwarder = register_forwarder(child_id)

    inbox: asyncio.Queue = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://server"
        ) as server_client:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {"agent": "researcher", "title": "task-1", "args": "do the thing"}
                ),
                server_client=server_client,
                conversation_id=parent_id,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=inbox,
            )

        assert isinstance(output, str) and output.startswith("Error"), (
            f"a failed child-message post must return a handled error (got {output!r})"
        )
        assert child_id in deletes, "teardown must delete the created child server-side"

        # The teardown deleted the server child but must also cancel the
        # runner-local forwarder; otherwise it keeps POSTing events to a
        # session the server has deleted once the reverse tunnel is down.
        assert forwarder.cancelled(), (
            "failed spawn leaked the child's transcript forwarder: teardown relied "
            "on a reverse-tunnel DELETE that never cancels it"
        )
        assert child_id not in orch._AUTO_FORWARDER_TASKS
    finally:
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        subagent_work.unregister_child_session(child_id)
        subagent_work.unregister_subagent_work(child_id)
        await drain_forwarder(child_id, forwarder)


@pytest.mark.asyncio
async def test_failed_send_to_continued_child_keeps_forwarder() -> None:
    parent_id = "conv_parent_continue"
    child_id = "conv_child_continue"
    deletes: list[str] = []
    patches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == f"/v1/sessions/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "agent_id": "agent_parent",
                    "root_conversation_id": parent_id,
                    "parent_session_id": None,
                },
            )
        # A continuable idle child already exists for (researcher, task-1); a
        # repeat send must continue it, not create a new one.
        if method == "GET" and path == f"/v1/sessions/{parent_id}/child_sessions":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": child_id,
                            "title": "researcher:task-1",
                            "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                            "busy": False,
                        }
                    ]
                },
            )
        # The dispatch-id stamp and the failed-send delivery receipt both PATCH
        # the child's labels.
        if method == "PATCH" and path == f"/v1/sessions/{child_id}":
            patches.append(path)
            return httpx.Response(200, json={"ok": True})
        # THE FAULT: the continued child's turn message POST fails.
        if method == "POST" and path == f"/v1/sessions/{child_id}/events":
            return httpx.Response(500, json={"error": "boom"})
        if method == "DELETE" and path.startswith("/v1/sessions/"):
            deletes.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"error": f"unmocked {method} {path}"})

    # The continued child's native pane is already running, so its forwarder
    # predates this send.
    forwarder = register_forwarder(child_id)

    inbox: asyncio.Queue = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://server"
        ) as server_client:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {"agent": "researcher", "title": "task-1", "args": "do the thing"}
                ),
                server_client=server_client,
                conversation_id=parent_id,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=inbox,
            )

        assert isinstance(output, str) and output.startswith("Error"), (
            f"a failed child-message post must return a handled error (got {output!r})"
        )
        # A continued child keeps its session, so teardown must NOT delete it
        # and must NOT reap its still-valid native forwarder.
        assert child_id not in deletes, "a continued child must not be deleted on a failed send"
        assert not forwarder.cancelled(), (
            "a failed send to a continued child wrongly reaped its live forwarder"
        )
        assert child_id in orch._AUTO_FORWARDER_TASKS
        assert patches, "teardown must still record the dispatch-id/receipt labels"
    finally:
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        subagent_work.unregister_child_session(child_id)
        subagent_work.unregister_subagent_work(child_id)
        await drain_forwarder(child_id, forwarder)
