"""Guards against sub-agent dispatch ``ReadTimeout`` and ``sys_session_close`` tombstone
both leak the spawned child OS-process cluster: the runner's session-DELETE
reaper is never reached on either path, so orphaned harness / tmux /
``claude_native_bridge`` clusters accumulate and eventually saturate the host.

Both tests drive the REAL runner dispatch entry point
(:func:`omnigent.runner.tool_dispatch.execute_tool`) against an
``httpx.MockTransport`` standing in for the Omnigent server, exactly as the
sibling ``tests/runner/test_runner_dispatch.py`` cases do. The mock injects
the report's fault (a create-time ``ReadTimeout`` after the server commits
child creation) and records every ``DELETE`` (the reaper) so the tests can
assert on what the dispatch code actually did.

Acceptance criteria:

1. After a create-timeout where the child turn never started, the dispatch
   MUST reach the runner session-DELETE reaper for the created child --
   otherwise its harness / ``omnigent-terminal-*`` tmux / ``claude_native_bridge``
   cluster is orphaned.
2. ``sys_session_close`` MUST reap the target's cluster (call the runner
   reaper -- a ``DELETE``, or an archive-equivalent ``PATCH archived=true``
   that schedules ``_spawn_archive_stop``), not merely PATCH ``closed=true``.

These tests fail when the reaper is never called (leaking build) and pass once the
timeout / tombstone paths properly wire to the session-DELETE lifecycle.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner.tool_dispatch import execute_tool

PARENT_ID = "conv_parent"
CHILD_ID = "conv_child_leaked"


@pytest.mark.asyncio
async def test_create_timeout_reaps_orphaned_child() -> None:
    """Facet 1 -- the create-timeout path must not leak the child cluster.

    The child-session create (``POST /v1/sessions``) commits server-side, then
    the caller's read deadline elapses -> ``httpx.ReadTimeout`` with no child
    id bound to the caller. The report's acceptance criterion (1): the dispatch
    must still reap the created child via the runner session-DELETE lifecycle,
    so no residual harness / tmux / bridge process remains.

    On the leaking build the create ``POST`` has a bare ``timeout=30.0`` with
    no ``ReadTimeout`` recovery and ``child_session_id`` is only bound after a
    successful response, so ``_teardown_failed_child`` (the DELETE reaper) can
    never fire -- the ReadTimeout propagates and ZERO DELETEs are issued.
    """
    deletes: list[str] = []
    created_ids: list[str] = []
    # Becomes True once the POST raises ReadTimeout, so the reconcile lookup
    # (child_sessions GET after the timeout) can return the committed session.
    post_committed: list[bool] = [False]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        # Parent snapshot -> resolves parent agent_id for dispatch.
        if method == "GET" and path == f"/v1/sessions/{PARENT_ID}":
            return httpx.Response(
                200,
                json={
                    "id": PARENT_ID,
                    "agent_id": "agent_parent",
                    "root_conversation_id": PARENT_ID,
                    "parent_session_id": None,
                },
            )
        # Child-session lookup: empty before the POST, returns the committed
        # session afterward so the ReadTimeout reconcile path can reap it.
        if method == "GET" and path == f"/v1/sessions/{PARENT_ID}/child_sessions":
            if not post_committed[0]:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": CHILD_ID,
                            "title": "researcher:task-1",
                            "root_conversation_id": PARENT_ID,
                            "parent_session_id": PARENT_ID,
                        }
                    ]
                },
            )
        # THE FAULT: creation commits on the server, but the caller's read
        # deadline elapses before it reads the response body.
        if method == "POST" and path == "/v1/sessions":
            created_ids.append(CHILD_ID)
            post_committed[0] = True
            raise httpx.ReadTimeout("read timed out", request=request)
        # The reaper the dispatch is supposed to reach for the orphaned child.
        if method == "DELETE" and path.startswith("/v1/sessions/"):
            deletes.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"error": f"unmocked {method} {path}"})

    inbox: asyncio.Queue = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_ID] = inbox
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {"agent": "researcher", "title": "task-1", "args": "do the thing"}
                ),
                server_client=server_client,
                conversation_id=PARENT_ID,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop(PARENT_ID, None)
            runner_app.unregister_child_session(CHILD_ID)
            runner_app.unregister_subagent_work(CHILD_ID)

    # The server committed a child; the dispatch must have surfaced the failure
    # as an error string (not raised an unhandled ReadTimeout to the caller).
    assert created_ids, "the mock server must have committed a child creation"
    assert isinstance(output, str) and output.startswith("Error"), (
        f"a create-timeout must return a handled error, not propagate ReadTimeout (got {output!r})"
    )
    # Acceptance criterion (1): the created child's cluster must be reaped.
    assert deletes, (
        "create-timeout leaked the child: the dispatch never called the runner "
        "session-DELETE reaper for the committed-but-unacked child, so its "
        "harness / omnigent-terminal tmux / claude_native_bridge cluster is orphaned"
    )


@pytest.mark.asyncio
async def test_tombstone_close_reaps_cluster() -> None:
    """Facet 2 -- ``sys_session_close`` must reap the target's cluster.

    On the leaking build ``_session_close_via_rest`` only issues a metadata
    PATCH (title rewrite + ``omnigent.closed=true`` label). It never sets
    ``archived=true`` (so the server's ``_spawn_archive_stop`` reaper never
    fires) and never issues the runner session-DELETE, so the child cluster is
    never reaped.

    Acceptance criterion (2): a tombstone must reach the reaper -- either a
    runner ``DELETE`` for the child, or an archive-equivalent
    ``PATCH archived=true`` that schedules the stop.
    """
    deletes: list[str] = []
    patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        # Caller snapshot (tree-scope check) + close-target snapshot.
        if method == "GET" and path == f"/v1/sessions/{PARENT_ID}":
            return httpx.Response(
                200,
                json={
                    "id": PARENT_ID,
                    "root_conversation_id": PARENT_ID,
                    "parent_session_id": None,
                },
            )
        if method == "GET" and path == f"/v1/sessions/{CHILD_ID}":
            return httpx.Response(
                200,
                json={
                    "id": CHILD_ID,
                    "title": "researcher:task-1",
                    "root_conversation_id": PARENT_ID,
                    "parent_session_id": PARENT_ID,
                },
            )
        # Runner session-DELETE reaper.
        if method == "DELETE" and path.startswith("/v1/sessions/"):
            deletes.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        # Metadata PATCH -- record body to inspect for an archive-equivalent stop.
        if method == "PATCH" and path == f"/v1/sessions/{CHILD_ID}":
            body = json.loads(request.content or b"{}")
            patches.append(body)
            return httpx.Response(200, json={"id": CHILD_ID, **body})
        return httpx.Response(404, json={"error": f"unmocked {method} {path}"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_close",
            arguments=json.dumps({"conversation_id": CHILD_ID}),
            server_client=server_client,
            conversation_id=PARENT_ID,
        )

    # The close reports success to the caller ...
    assert json.loads(output).get("closed") is True, (
        f"close should report success (got {output!r})"
    )
    # ... but that success MUST have reached the reaper. Either a direct runner
    # DELETE for the child, or an archive-equivalent PATCH that schedules the
    # stop (_spawn_archive_stop is gated on archived is True).
    reaped_via_delete = CHILD_ID in deletes
    reaped_via_archive = any(body.get("archived") is True for body in patches)
    assert reaped_via_delete or reaped_via_archive, (
        "sys_session_close tombstoned the child with a metadata-only PATCH "
        f"({patches}) -- it set no archived=true and issued no runner DELETE, "
        "so the child's harness / tmux / bridge cluster is never reaped"
    )
