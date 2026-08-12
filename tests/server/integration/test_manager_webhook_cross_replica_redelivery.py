"""Cross-replica decided-elicitation delivery for a continuously-connected
runner (OMN-104 §5.4 case (b), cross-vendor review P1).

``_on_runner_connect`` only redelivers decided-but-undelivered verdicts on
an actual tunnel RECONNECT. Before this fix, a decision recorded on a
replica that does NOT own the runner's live tunnel was a dead end whenever
the runner stayed continuously connected to another replica the whole
time — no reconnect ever fires there, so ``pending_redelivery`` was a
promise the codebase had no mechanism to keep.

This file builds TWO genuinely independent ``create_app()`` instances
sharing only the database — "replica A" holds a real WS tunnel to a real
runner app (the exact machinery ``test_sessions_tunnel_three_layer.py``
uses) and has its real ASGI lifespan driven so its background
``_redelivery_sweep_loop`` task actually runs; "replica B" has no tunnel
connection for this runner at all. The decision is POSTed through replica
B. The tunnel is NEVER dropped or reconnected during the test — delivery
must happen via replica A's periodic sweep alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import omnigent.server.app as app_module
from omnigent.runner import pending_approvals
from omnigent.runner.app import create_runner_app
from omnigent.runtime import init as init_runtime
from omnigent.runtime import (
    set_harness_process_manager,
    set_runner_id,
    set_runner_router,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.server import session_outbox
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.runner.helpers import NullServerClient
from tests.server.integration.test_sessions_tunnel_three_layer import (
    _RUNNER_ID,
    _TEST_HARNESS_NAME,
    FakeProcessManager,
    _build_harness_agent_bundle,
    _connect_runner_tunnel,
    _forward_requests_to_runner,
    _send_hello_and_wait,
)

pytestmark = pytest.mark.asyncio

# Fast enough that the test doesn't sit through the real 5s production
# interval, slow enough to leave headroom above scheduling jitter.
_TEST_SWEEP_INTERVAL_S = 0.2


@dataclass
class _CrossReplicaStack:
    client_a: httpx.AsyncClient
    app_a: FastAPI
    client_b: httpx.AsyncClient
    app_b: FastAPI
    db_uri: str


@pytest_asyncio.fixture()
async def cross_replica_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_CrossReplicaStack]:
    """Two independent Omnigent replicas sharing one DB; only replica A
    holds the real runner tunnel, and only replica A's ASGI lifespan (and
    therefore its ``_redelivery_sweep_loop`` task) is driven."""
    monkeypatch.setattr(app_module, "_REDELIVERY_SWEEP_INTERVAL_S", _TEST_SWEEP_INTERVAL_S)

    db_uri = f"sqlite:///{tmp_path / 'test.db'}"
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    # ONE artifact_store/agent_cache pair shared by init_runtime, the
    # runner's spec resolver, AND replica A's own create_app — replica A is
    # the one whose client uploads the agent bundle in the test, and the
    # runner (a separate process in production, here a separate ASGI app)
    # must resolve specs from the SAME artifact storage that bundle landed
    # in. Mirrors tunnel_three_layer_stack's single-artifact-store pattern.
    artifact_store_a = LocalArtifactStore(str(tmp_path / "replica_a" / "artifacts"))
    agent_cache_a = AgentCache(
        artifact_store=artifact_store_a,
        cache_dir=tmp_path / "replica_a" / "cache",
    )

    init_runtime(
        conversation_store=SqlAlchemyConversationStore(db_uri),
        agent_store=agent_store,
        agent_cache=agent_cache_a,
        file_store=file_store,
        artifact_store=artifact_store_a,
    )

    saved_harness_module = _HARNESS_MODULES.get(_TEST_HARNESS_NAME)
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = "tests.runtime.harnesses._test_scaffold_harnesses"

    fake_pm = FakeProcessManager()
    set_harness_process_manager(fake_pm)
    set_runner_id(_RUNNER_ID)

    async def _resolve_spec(agent_id: str, session_id: str | None = None) -> Any:
        del session_id
        record = agent_store.get(agent_id)
        if record is None:
            return None
        return agent_cache_a.load(agent_id, record.bundle_location).spec

    runner_app = create_runner_app(
        process_manager=fake_pm,
        spec_resolver=_resolve_spec,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    app_a = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store_a,
        agent_cache=agent_cache_a,
    )
    set_runner_router(app_a.state.runner_router)

    artifact_store_b = LocalArtifactStore(str(tmp_path / "replica_b" / "artifacts"))
    app_b = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store_b,
        agent_cache=AgentCache(
            artifact_store=artifact_store_b, cache_dir=tmp_path / "replica_b" / "cache"
        ),
    )

    # Real WS tunnel to REPLICA A ONLY -- replica B never sees this runner.
    communicator = await _connect_runner_tunnel(app_a, _RUNNER_ID)
    await _send_hello_and_wait(communicator, app_a, _RUNNER_ID, harnesses=[_TEST_HARNESS_NAME])
    forwarder_task = asyncio.create_task(
        _forward_requests_to_runner(communicator, runner_app),
        name="cross-replica-forwarder",
    )

    client_a = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_a), base_url="http://replica-a"
    )
    client_b = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="http://replica-b"
    )

    # Drive replica A's REAL ASGI lifespan so its _redelivery_sweep_loop
    # background task actually starts -- this is the real production entry
    # point under test, not a reimplementation of the sweep logic.
    async with app_a.router.lifespan_context(app_a):
        try:
            yield _CrossReplicaStack(
                client_a=client_a, app_a=app_a, client_b=client_b, app_b=app_b, db_uri=db_uri
            )
        finally:
            loop_tasks = []
            for task in asyncio.all_tasks():
                if task is asyncio.current_task():
                    continue
                coro_qualname = getattr(task.get_coro(), "__qualname__", "")
                if (
                    task.get_name().startswith("runner-relay-")
                    or task.get_name().startswith("runner-disconnect-grace-")
                    or "_relay_runner_stream" in coro_qualname
                ):
                    loop_tasks.append(task)
            for t in loop_tasks:
                t.cancel()
            for t in loop_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

            await client_a.aclose()
            await client_b.aclose()

            forwarder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forwarder_task

            with contextlib.suppress(asyncio.CancelledError, Exception):
                await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await communicator.wait(timeout=2.0)

            with contextlib.suppress(Exception):
                await app_a.state.runner_router.aclose()
            with contextlib.suppress(Exception):
                await app_b.state.runner_router.aclose()

            await fake_pm.shutdown()
            set_runner_router(None)
            set_runner_id(None)
            set_harness_process_manager(None)
            if saved_harness_module is None:
                _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)
            else:
                _HARNESS_MODULES[_TEST_HARNESS_NAME] = saved_harness_module
            pending_approvals.reset_for_tests()


async def test_decision_on_non_tunnel_replica_reaches_continuously_connected_runner(
    cross_replica_stack: _CrossReplicaStack,
) -> None:
    """
    A decision POSTed to replica B (which holds no tunnel for this runner)
    reaches the runner that stays CONTINUOUSLY connected to replica A the
    entire time — no disconnect, no reconnect, ever, in this test. Delivery
    must happen via replica A's periodic redelivery sweep, and
    ``session.resumed`` must be recorded exactly once.
    """
    stack = cross_replica_stack
    app_a = stack.app_a

    create_resp = await stack.client_a.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": ("agent.tar.gz", _build_harness_agent_bundle(), "application/gzip"),
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["session_id"]

    bind_resp = await stack.client_a.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": _RUNNER_ID},
    )
    assert bind_resp.status_code == 200, bind_resp.text

    # touch_runner_liveness at real tunnel-connect time was a no-op — it
    # bulk-UPDATEs rows WHERE runner_id == _RUNNER_ID, and zero sessions
    # were bound to this runner yet (the session above didn't exist until
    # after the tunnel connected in the fixture). In production the next
    # tunnel ping cycle (runner_tunnel._ping_loop) re-touches liveness for
    # every session bound by then; stamping it directly here establishes
    # "runner known-alive" ground truth without depending on that ping
    # loop's timing, same as test_manager_webhook_decision_endpoint.py's
    # own cross-replica test does.
    SqlAlchemyConversationStore(stack.db_uri).touch_runner_liveness([_RUNNER_ID], int(time.time()))

    elicitation_id = "elicit_cross_replica_test"
    future = pending_approvals.register(elicitation_id)
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    # POST the decision through REPLICA B, which holds no tunnel for this
    # runner at all -- must be recorded durably but NOT delivered
    # synchronously, and classified as pending-redelivery (the runner is
    # believed alive: replica A's tunnel connect already stamped fresh
    # liveness in the shared DB).
    verdict = await stack.client_b.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    body = verdict.json()
    assert body["resolved"] is False
    assert body["pending_redelivery"] is True

    # Delivery must arrive via replica A's periodic sweep -- no reconnect
    # is ever driven in this test.
    approved = await asyncio.wait_for(future, timeout=10.0)
    assert approved is True, "real pending_approvals Future did not resolve to accept"

    store = app_a.state.session_lifecycle_store
    resumed: list[Any] = []
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        deliveries, _ = store.list_deliveries(session_id, limit=100)
        resumed = [d for d in deliveries if d.event_type == "session.resumed"]
        if resumed:
            break
        await asyncio.sleep(0.05)
    assert len(resumed) == 1, "expected exactly one session.resumed delivery"

    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "delivered_to_runner"
