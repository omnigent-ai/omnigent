"""Failed named sends preserve uncertain work under confined credentials.

Local transports cover legacy cleanup, scoped timeout retention and explicit
session close. No runner process or external server is started.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import subagent_work
from omnigent.runner.tool_dispatch import execute_tool

PARENT_ID = "conv_parent"
CHILD_ID = "conv_child_leaked"


@pytest.mark.asyncio
@pytest.mark.parametrize("confined", [False, True])
async def test_create_timeout_cleanup_respects_confined_credentials(confined: bool) -> None:
    """Legacy cleanup remains; confined timeouts never reconcile by mutable title."""
    deletes: list[str] = []
    created_ids: list[str] = []
    # Becomes True once the POST raises ReadTimeout, so the reconcile lookup
    # (child_sessions GET after the timeout) can return the committed session.
    post_committed: list[bool] = [False]
    timeout_reconciliations: list[str] = []

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
            timeout_reconciliations.append(path)
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
    subagent_work._session_inboxes_ref[PARENT_ID] = inbox
    auth = httpx.Auth()
    auth.runner_confined = confined
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server", auth=auth
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
            subagent_work._session_inboxes_ref.pop(PARENT_ID, None)
            subagent_work.unregister_child_session(CHILD_ID)
            subagent_work.unregister_subagent_work(CHILD_ID)

    # The server committed a child; the dispatch must have surfaced the failure
    # as an error string (not raised an unhandled ReadTimeout to the caller).
    assert created_ids, "the mock server must have committed a child creation"
    assert isinstance(output, str) and output.startswith("Error"), (
        f"a create-timeout must return a handled error, not propagate ReadTimeout (got {output!r})"
    )
    if confined:
        assert not deletes
        assert not timeout_reconciliations
        assert "unknown" in output and "automatic cleanup is unavailable" in output
        return
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


@pytest.mark.asyncio
@pytest.mark.parametrize("created_child", [False, True])
async def test_confined_failed_send_retains_work_without_cleanup(created_child: bool) -> None:
    from omnigent.runner.tool_dispatch import _teardown_failed_child

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    auth = httpx.Auth()
    auth.runner_confined = True
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_ID,
        child_session_id=CHILD_ID,
        agent="researcher",
        title="task-1",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://server", auth=auth
        ) as client:
            warning = await _teardown_failed_child(client, CHILD_ID, created_child=created_child)
        assert warning is not None and CHILD_ID in warning and "retained" in warning
        assert subagent_work.get_subagent_work(CHILD_ID) is entry
        assert requests == []
    finally:
        subagent_work.unregister_subagent_work(CHILD_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "prepare-lost",
        "prepare-unknown",
        "input-lost",
        "input-rejected",
        "abort-lost",
        "input-unknown",
        "input-cancelled",
        "mapping",
    ],
)
async def test_pending_spawn_preserves_exact_receipt_and_work(fault: str | None) -> None:
    from uuid import uuid4

    parent, child, source, copied = (uuid4().hex for _ in range(4))
    path = f"/v1/sessions/{parent}/spawns"
    receipt = {}
    calls = []
    inputs = []
    prepares = []
    state = "pending"

    def handle(request):
        nonlocal state
        route, method = request.url.path, request.method
        calls.append((method, route))
        if method == "GET" and route == "/v1/info":
            return httpx.Response(200, json={"pending_child_spawn_v1": True})
        if method == "GET" and route == f"/v1/sessions/{parent}":
            return httpx.Response(200, json={"id": parent, "agent_id": "parent-agent"})
        if method == "GET" and route == f"/v1/sessions/{parent}/child_sessions":
            assert not prepares, "Never reconcile a pending spawn by title"
            return httpx.Response(200, json={"data": []})
        if method == "GET" and route == f"{path}/epoch":
            return httpx.Response(200, json={"epoch": 1})
        if method == "POST" and route == path:
            payload = json.loads(request.content)
            prepares.append(payload)
            assert subagent_work.get_subagent_work(child) is None
            assert payload["cost_budget"] == {"max_cost_usd": 2.0}
            assert payload["file_ids"] == [source]
            receipt.update(
                {
                    "epoch": 1,
                    "nonce": payload["nonce"],
                    "child_id": child,
                    "files": {
                        source: {
                            "new_id": copied,
                            "content_type": "image/png",
                            "filename": "photo.png",
                        }
                    },
                    "labels": {},
                }
            )
            if fault == "mapping":
                receipt["files"] = {}
            if fault in ("prepare-lost", "prepare-unknown"):
                raise httpx.ReadTimeout("lost prepare response", request=request)
            return httpx.Response(201, json={**receipt, "state": state})
        if method == "GET" and route == f"{path}/{receipt['nonce']}":
            assert request.url.params["epoch"] == "1"
            if fault == "input-unknown":
                return httpx.Response(503)
            return httpx.Response(
                200, json={**receipt, "state": "unknown" if fault == "prepare-unknown" else state}
            )
        if method == "POST" and route == f"{path}/{receipt['nonce']}/input":
            assert subagent_work.get_subagent_work(child) is not None
            inputs.append(json.loads(request.content))
            if fault == "input-cancelled":
                raise asyncio.CancelledError()
            if fault in ("input-rejected", "abort-lost"):
                return httpx.Response(503)
            state = "admitted"
            if fault in ("input-lost", "input-unknown"):
                raise httpx.ReadTimeout("lost input response", request=request)
            return httpx.Response(202, json={**receipt, "state": state})
        if method == "POST" and route == f"{path}/{receipt['nonce']}/abort":
            if fault == "input-unknown":
                return httpx.Response(503)
            if state == "pending":
                state = "aborted"
            if fault == "abort-lost":
                raise httpx.ReadTimeout("lost abort response", request=request)
            return httpx.Response(200, json={**receipt, "state": state})
        raise AssertionError(f"Unexpected request: {method} {route}")

    auth = httpx.Auth()
    auth.runner_confined = True
    inbox = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent] = inbox
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://server", auth=auth
        ) as client:
            call = execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "researcher",
                        "title": "task",
                        "args": {
                            "input": "first",
                            "file_ids": [source],
                            "cost_budget": {"max_cost_usd": 2.0},
                        },
                    }
                ),
                server_client=client,
                conversation_id=parent,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=inbox,
            )
            if fault == "input-cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await call
                output = "cancelled"
            else:
                output = await call
        assert len(prepares) == 1
        assert len(inputs) == (0 if fault in ("mapping", "prepare-unknown") else 1)
        if inputs:
            assert inputs[0]["input"]["data"]["content"] == [
                {"type": "input_text", "text": "first"},
                {"type": "input_image", "file_id": copied},
            ]
        if fault in (None, "prepare-lost", "input-lost"):
            assert json.loads(output)["conversation_id"] == child
            assert state == "admitted"
            assert subagent_work.get_subagent_work(child) is not None
        elif fault == "input-unknown":
            assert "Retain" in output
            assert subagent_work.get_subagent_work(child) is not None
        else:
            assert output.startswith("Error") or output == "cancelled"
            assert subagent_work.get_subagent_work(child) is None
            if fault != "prepare-unknown":
                assert state == "aborted"
        assert not any(method == "DELETE" for method, _ in calls)
        assert ("POST", "/v1/sessions") not in calls
    finally:
        subagent_work._session_inboxes_ref.pop(parent, None)
        subagent_work.unregister_child_session(child)
        subagent_work.unregister_subagent_work(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["retry", "explicit", "exhausted", "generic", "mismatch"])
async def test_pending_spawn_retries_only_definite_auto_name_rejection(outcome: str) -> None:
    from uuid import uuid4

    parent, child = uuid4().hex, uuid4().hex
    path = f"/v1/sessions/{parent}/spawns"
    prepares = []
    reconciles = []

    def handle(request):
        route, method = request.url.path, request.method
        if method == "GET" and route == "/v1/info":
            return httpx.Response(200, json={"pending_child_spawn_v1": True})
        if method == "GET" and route == f"/v1/sessions/{parent}":
            return httpx.Response(200, json={"id": parent, "agent_id": "parent-agent"})
        if method == "GET" and route == f"/v1/sessions/{parent}/child_sessions":
            assert not prepares
            return httpx.Response(200, json={"data": []})
        if method == "GET" and route == f"{path}/epoch":
            return httpx.Response(200, json={"epoch": 1})
        if method == "POST" and route == path:
            payload = json.loads(request.content)
            prepares.append(payload)
            if outcome == "retry" and len(prepares) == 2:
                return httpx.Response(
                    201,
                    json={
                        "epoch": 1,
                        "nonce": payload["nonce"],
                        "child_id": child,
                        "state": "pending",
                        "files": {},
                        "labels": {},
                    },
                )
            rejection = {
                "epoch": 1,
                "nonce": payload["nonce"],
                "state": "rejected",
                "reason": "name_conflict",
            }
            if outcome == "generic":
                rejection = {"error": "conflict"}
            elif outcome == "mismatch":
                rejection["nonce"] = uuid4().hex
            return httpx.Response(409, json=rejection)
        latest = prepares[-1]
        if method == "GET" and route == f"{path}/{latest['nonce']}":
            reconciles.append(route)
            return httpx.Response(
                200, json={"epoch": 1, "nonce": latest["nonce"], "state": "unknown"}
            )
        if method == "POST" and route == f"{path}/{latest['nonce']}/input":
            assert outcome == "retry"
            return httpx.Response(
                202,
                json={
                    "epoch": 1,
                    "nonce": latest["nonce"],
                    "child_id": child,
                    "state": "admitted",
                },
            )
        raise AssertionError(f"Unexpected request: {method} {route}")

    auth = httpx.Auth()
    auth.runner_confined = True
    inbox = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent] = inbox
    arguments = {"agent": "researcher", "args": {"input": "first"}}
    if outcome == "explicit":
        arguments["title"] = "named"
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://server", auth=auth
        ) as client:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id=parent,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=inbox,
            )
        assert len(prepares) == {"retry": 2, "exhausted": 6}.get(outcome, 1)
        assert len({item["nonce"] for item in prepares}) == len(prepares)
        assert len({item["create"]["title"] for item in prepares}) == len(prepares)
        assert len(reconciles) == (1 if outcome in ("generic", "mismatch") else 0)
        if outcome == "retry":
            assert json.loads(output)["conversation_id"] == child
        else:
            assert output.startswith("Error")
    finally:
        subagent_work._session_inboxes_ref.pop(parent, None)
        subagent_work.unregister_child_session(child)
        subagent_work.unregister_subagent_work(child)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload", [(503, {}), (401, {}), (200, []), (200, {"pending_child_spawn_v1": "true"})]
)
async def test_pending_spawn_discovery_failure_never_falls_back(status, payload):
    from uuid import uuid4

    from omnigent.runner.pending_spawns import PendingSpawnError, prepare_pending_spawn

    def handle(request):
        assert request.method == "GET" and request.url.path == "/v1/info"
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        with pytest.raises(PendingSpawnError, match="Could not determine"):
            await prepare_pending_spawn(
                client, parent_id=uuid4().hex, create={}, file_ids=[], cost_budget=None
            )
