"""Accepted managed input uses normal dispatch after the creating request is gone."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.datastructures import Headers

from omnigent.entities.conversation import ErrorData, MessageData, NewConversationItem
from omnigent.entities.scheduled_task import ScheduledRunLogContext
from omnigent.harness_plugins import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
    KIRO_NATIVE_CODING_AGENT,
)
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime import pending_inputs
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.server.routes._sessions.helpers import _HostLaunchAttempt
from omnigent.server.routes.sessions import routes_core
from omnigent.server.scheduled.telemetry import scheduled_run_scope
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import (
    STARTUP_LABEL,
    ManagedStartupInputs,
    StartupInputContext,
    wake_startup_worker,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle, create_test_agent
from tests.server.integration.test_managed_launch_failure_teardown import (
    _Env,
)
from tests.server.integration.test_managed_launch_failure_teardown import (
    teardown_env as teardown_env,
)
from tests.server.integration.test_sessions_native_kickoff_duplicate import (
    _simulate_transcript_forwarder_echo,
)
from tests.server.integration.test_sessions_web_send_stable_id import _stub_runner

pytestmark = pytest.mark.asyncio
_TEXT = "Run the saved first message after the browser has gone."
_INPUT = {
    "type": "message",
    "data": {"role": "user", "content": [{"type": "input_text", "text": _TEXT}]},
}


@pytest.fixture
def scheduled_run_context() -> ScheduledRunLogContext:
    return ScheduledRunLogContext(
        task_id=uuid4().hex,
        run_id=uuid4().hex,
        task_workspace_id=0,
        scheduled_at=1000,
        trigger_key="schedule:1000",
        target="managed_sandbox",
        session_mode="new",
    )


@pytest.fixture
def startup_permissions(db_uri: str) -> SqlAlchemyPermissionStore:
    return SqlAlchemyPermissionStore(db_uri)


@pytest.fixture
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    startup_permissions: SqlAlchemyPermissionStore,
) -> FastAPI:
    """The shared auth-enabled app, with managed sandboxes configured."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=startup_permissions,
        auth_provider=UnifiedAuthProvider(
            source="header", local_single_user=getattr(request, "param", False)
        ),
        host_store=HostStore(db_uri),
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
            }
        ),
    )


@pytest_asyncio.fixture
async def startup_env(teardown_env: _Env, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Env]:
    tasks: set[asyncio.Task[Any]] = set()

    async def wait_for_provisioning(**_: Any) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        await asyncio.Future()

    async def relay_ready(*_: Any, **__: Any) -> None:
        pass

    monkeypatch.setattr(routes_core, "_run_managed_launch", wait_for_provisioning)
    monkeypatch.setattr("omnigent.server.routes.sessions._ensure_runner_relay_ready", relay_ready)
    yield teardown_env
    await teardown_env.app.state.startup_inputs.shutdown()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _create(env: _Env, *, name: str = "startup-probe", **options: Any) -> str:
    agent = await create_test_agent(env.client, name=name)
    response = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed", "initial_input": _INPUT, **options},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _bind(env: _Env, session_id: str):
    env.conv_store.set_host_id(session_id, uuid4().hex, "/tmp/managed-workspace")
    return env.conv_store.list_pending_inputs(session_id)[0]


@pytest.mark.parametrize(
    "name,native",
    [
        ("startup-probe", False),
        (CLAUDE_NATIVE_CODING_AGENT.agent_name, True),
        (CODEX_NATIVE_CODING_AGENT.agent_name, True),
    ],
)
@pytest.mark.parametrize("scheduled", [False, True])
async def test_saved_input_survives_service_recreation_and_dispatches_once(
    startup_env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    native: bool,
    scheduled,
    scheduled_run_context,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    env = startup_env
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        with scheduled_run_scope(scheduled_run_context if scheduled else None):
            session_id = await _create(env, name=name)
        original = env.app.state.startup_inputs
        entry = env.conv_store.list_pending_inputs(session_id)[0]
        assert entry.status == "in_progress"
        assert entry.data.startup_options.scheduled_run == (
            scheduled_run_context if scheduled else None
        )
        assert not [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_sent"
        ]
        snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
        assert snapshot["items"] == [], "accepted input must not be seeded as history"
        assert snapshot["pending_inputs"][0]["content"] == _INPUT["data"]["content"]
        pending_id = snapshot["pending_inputs"][0]["pending_id"]
        assert not [event for event in forwarded if event.get("type") == "message"]

        entry = _bind(env, session_id)
        restarted = ManagedStartupInputs(env.conv_store, original.hosts)
        restarted.dispatch = original.dispatch
        await asyncio.gather(
            original._deliver(env.app, session_id, entry),
            restarted._deliver(env.app, session_id, entry),
        )
        turns = [event for event in forwarded if event.get("type") == "message"]
        assert len(turns) == 1 and _TEXT in json.dumps(turns[0])
        await restarted._deliver(env.app, session_id, entry)
        assert len([event for event in forwarded if event.get("type") == "message"]) == 1
        sent = [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_sent"
        ]
        assert len(sent) == int(scheduled)
        if scheduled:
            assert sent[0].attributes.items() >= scheduled_run_context.model_dump().items()
            assert sent[0].attributes["input_id"] == entry.id
            assert sent[0].session_id == session_id
            assert scheduled_run_context.run_id not in json.dumps(turns)

        snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
        if native:
            assert not [item for item in snapshot["items"] if item["type"] == "message"]
            assert snapshot["pending_inputs"][0]["pending_id"] == pending_id
            pending_inputs.reset_for_tests()
            await _simulate_transcript_forwarder_echo(env.client, session_id, _TEXT)
        snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
        assert len([item for item in snapshot["items"] if item["type"] == "message"]) == 1
        assert snapshot["pending_inputs"] == []
    finally:
        await runner.aclose()


async def test_refused_delivery_retains_input_until_explicit_retry(
    startup_env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    scheduled_run_context,
    caplog,
    debug_log_rows,
) -> None:
    caplog.set_level(logging.INFO)
    env = startup_env
    with scheduled_run_scope(scheduled_run_context):
        session_id = await _create(env)
    entry = _bind(env, session_id)
    startup = env.app.state.startup_inputs
    denied = True

    async def evaluate(*_: Any, **__: Any):
        return {"verdict": "deny", "reason": "Blocked test input"} if denied else None

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_events._evaluate_input_policy", evaluate
    )
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        await startup._deliver(env.app, session_id, entry)
        assert env.conv_store.list_pending_inputs(session_id)[0].status == "failed"
        assert not [event for event in forwarded if event.get("type") == "message"]
        assert not [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_sent"
        ]
        [failure] = [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_stage_failed"
        ]
        assert failure.attributes["run_id"] == scheduled_run_context.run_id
        assert failure.attributes["outcome"] == "not_sent"
        assert failure.attributes["failure_stage"] == "input_policy"
        assert failure.attributes["input_id"] == entry.id
        [refused_attempt] = [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_dispatch_started"
        ]
        refused_dispatch_id = failure.attributes["dispatch_id"]
        assert refused_dispatch_id
        assert refused_attempt.attributes["dispatch_id"] == refused_dispatch_id
        assert refused_attempt.attributes["input_id"] == entry.id
        [row] = [
            row
            for row in debug_log_rows
            if row["message"].startswith("Unable to deliver saved message")
        ]
        assert row["session_id"] == session_id
        assert row["attributes"]["run_id"] == scheduled_run_context.run_id
        snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
        assert any(
            item["type"] == "error" and item["data"]["code"] == "startup_input_failed"
            for item in snapshot["items"]
        )
        denied = False
        env.conv_store.set_runner_id(session_id, uuid4().hex)
        retried = await env.client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "retry_session"}
        )
        assert retried.status_code == 202, retried.text
        await startup._deliver(
            env.app, session_id, env.conv_store.list_pending_inputs(session_id)[0]
        )
        assert env.conv_store.list_pending_inputs(session_id) == []
        assert len([event for event in forwarded if event.get("type") == "message"]) == 1
        [sent] = [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_sent"
        ]
        [first_attempt, retry_attempt] = [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_dispatch_started"
        ]
        assert first_attempt is refused_attempt
        assert retry_attempt.attributes["input_id"] == sent.attributes["input_id"] == entry.id
        retry_dispatch_id = retry_attempt.attributes["dispatch_id"]
        assert retry_dispatch_id and retry_dispatch_id != refused_dispatch_id
        assert sent.attributes["dispatch_id"] == retry_dispatch_id
        assert sent.attributes["run_id"] == scheduled_run_context.run_id
    finally:
        await runner.aclose()


@pytest.mark.parametrize("bundled", [False, True])
async def test_multipart_input_keeps_attachment_in_session_namespace(
    startup_env: _Env, db_uri: str, monkeypatch: pytest.MonkeyPatch, bundled: bool
) -> None:
    env = startup_env
    agent = await create_test_agent(env.client)
    metadata = {"host_type": "managed", "initial_input": _INPUT}
    files = [("initial_files", ("context.txt", b"context that outlives the tab", "text/plain"))]
    if bundled:
        files.append(
            ("bundle", ("agent.tar.gz", build_agent_bundle("bundled"), "application/gzip"))
        )
    else:
        metadata["agent_id"] = agent["id"]
    response = await env.client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files=files,
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["session_id" if bundled else "id"]
    snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
    blocks = snapshot["pending_inputs"][0]["content"]
    assert blocks[0]["type"] == "input_file" and blocks[0]["filename"] == "context.txt"
    stored = SqlAlchemyFileStore(db_uri).get(blocks[0]["file_id"], session_id=session_id)
    assert stored is not None and stored.session_id == session_id
    downloaded = await env.client.get(
        f"/v1/sessions/{session_id}/resources/files/{stored.id}/content"
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"context that outlives the tab"
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        await env.app.state.startup_inputs._deliver(env.app, session_id, _bind(env, session_id))
    finally:
        await runner.aclose()
    turns = [event for event in forwarded if event.get("type") == "message"]
    assert len(turns) == 1
    file_data = turns[0]["content"][0]["file_data"]
    assert base64.b64decode(file_data.split(",", 1)[1]) == b"context that outlives the tab"


@pytest.mark.parametrize(
    "options",
    [
        {"host_type": "external"},
        {"initial_items": [_INPUT]},
        {"initial_input": {"type": "interrupt", "data": {}}},
        {"initial_input": {**_INPUT, "created_by": "someone-else"}},
        {"initial_input": {"type": "message", "data": {"role": "assistant", "content": []}}},
        {"initial_input": {"type": "slash_command", "data": {"kind": "shell", "name": "x"}}},
    ],
)
async def test_invalid_startup_input_is_refused(
    startup_env: _Env, options: dict[str, Any]
) -> None:
    agent = await create_test_agent(startup_env.client)
    response = await startup_env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed", "initial_input": _INPUT, **options},
    )
    assert response.status_code == 422, response.text


async def test_deleting_provisioning_session_discards_saved_input(startup_env: _Env) -> None:
    env = startup_env
    session_id = await _create(env)
    assert env.conv_store.list_pending_inputs(session_id)
    response = await env.client.delete(f"/v1/sessions/{session_id}")
    assert response.status_code == 200, response.text
    assert env.conv_store.list_pending_inputs(session_id) == []


async def test_cancelled_worker_leaves_unsent_input_for_restart(startup_env: _Env) -> None:
    env = startup_env
    session_id = await _create(env)
    entry = _bind(env, session_id)
    startup = env.app.state.startup_inputs
    entered = asyncio.Event()

    async def slow_dispatch(*_: Any) -> dict[str, bool | str]:
        entered.set()
        await asyncio.Future()
        return {"queued": True}

    startup.dispatch = slow_dispatch
    delivery = asyncio.create_task(startup._deliver(env.app, session_id, entry))
    await asyncio.wait_for(entered.wait(), timeout=5)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    assert env.conv_store.list_pending_inputs(session_id)[0].status == "in_progress"


@pytest.mark.parametrize("control", ["interrupt", "stop_session"])
async def test_stop_before_host_ready_cancels_all_saved_inputs(
    startup_env: _Env, control: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    session_id = await _create(env)
    startup = env.app.state.startup_inputs
    for _ in range(2):
        followup = await env.client.post(f"/v1/sessions/{session_id}/events", json=_INPUT)
        assert followup.status_code == 202, followup.text
    assert len(env.conv_store.list_pending_inputs(session_id)) == 3
    entry = env.conv_store.list_pending_inputs(session_id)[0]
    response = await env.client.post(f"/v1/sessions/{session_id}/events", json={"type": control})
    assert response.status_code == 202, response.text
    assert env.conv_store.list_pending_inputs(session_id) == []
    # SDK startup/reconnect loads this endpoint and resumes a trailing user item.
    history = await env.client.get(f"/v1/sessions/{session_id}/items")
    assert history.status_code == 200, history.text
    assert history.json()["data"] == [], "Stop must not turn queued input into recovery history"
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        await startup._deliver(env.app, session_id, entry)
    finally:
        await runner.aclose()
    assert forwarded == []


async def test_stop_cancels_local_dispatch_and_clears_saved_input(
    startup_env: _Env, scheduled_run_context, caplog
) -> None:
    caplog.set_level(logging.INFO)
    env = startup_env
    with scheduled_run_scope(scheduled_run_context):
        session_id = await _create(env)
    startup = env.app.state.startup_inputs
    entry = _bind(env, session_id)
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def dispatch(*_: Any) -> dict[str, bool | str]:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
        return {"queued": True}

    startup.dispatch = dispatch
    delivery = asyncio.create_task(startup._deliver(env.app, session_id, entry))
    await asyncio.wait_for(entered.wait(), timeout=5)
    response = await env.client.post(
        f"/v1/sessions/{session_id}/events", json={"type": "interrupt"}
    )
    assert response.status_code == 202, response.text
    assert stopped.is_set() and delivery.cancelled()
    assert env.conv_store.list_pending_inputs(session_id) == []

    [interrupted] = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "scheduled_run_prompt_dispatch_interrupted"
    ]
    assert interrupted.attributes["run_id"] == scheduled_run_context.run_id
    assert interrupted.attributes["input_id"] == entry.id
    assert interrupted.attributes["outcome"] == "not_sent"
    assert not any(
        getattr(r, "event_name", None) == "scheduled_run_prompt_sent" for r in caplog.records
    )


async def test_uncertain_native_submission_is_not_retried_on_restart(
    startup_env: _Env, scheduled_run_context, caplog
) -> None:
    caplog.set_level(logging.INFO)
    env = startup_env
    with scheduled_run_scope(scheduled_run_context):
        session_id = await _create(env, name=CODEX_NATIVE_CODING_AGENT.agent_name)
    entry = _bind(env, session_id)
    startup = env.app.state.startup_inputs
    entered = asyncio.Event()

    async def dispatch(context: StartupInputContext, session_id: str, _: SessionEventInput):
        assert await context.claim(env.conv_store, session_id)
        entered.set()
        await asyncio.Future()
        return {"queued": True}

    startup.dispatch = dispatch
    delivery = asyncio.create_task(startup._deliver(env.app, session_id, entry))
    await asyncio.wait_for(entered.wait(), timeout=5)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    assert env.conv_store.list_pending_inputs(session_id)[0].status == "incomplete"
    assert not [
        r for r in caplog.records if getattr(r, "event_name", None) == "scheduled_run_prompt_sent"
    ]
    [interrupted] = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "scheduled_run_prompt_dispatch_interrupted"
    ]
    assert interrupted.attributes["run_id"] == scheduled_run_context.run_id
    assert interrupted.attributes["outcome"] == "uncertain"
    entered.clear()
    await startup._deliver(env.app, session_id, entry)
    assert not entered.is_set()
    assert not await startup.retry(session_id)
    pending_inputs.reset_for_tests()
    await _simulate_transcript_forwarder_echo(env.client, session_id, _TEXT)
    assert env.conv_store.list_pending_inputs(session_id) == []


@pytest.mark.parametrize(
    "auth_app,user_id,revoke_access",
    [(False, "alice", False), (False, "alice", True), (True, "local", False)],
    indirect=["auth_app"],
    ids=["named-user", "revoked-user", "local-user"],
)
async def test_queued_input_preserves_identity_and_rechecks_access(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    startup_permissions: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
    user_id: str,
    revoke_access: bool,
) -> None:
    if user_id != "local":
        auth_client.headers["X-Forwarded-Email"] = user_id
    agent = await create_test_agent(auth_client)
    response = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    startup = auth_app.state.startup_inputs
    startup.conversations.set_labels(session_id, {STARTUP_LABEL: "pending"})
    response = await auth_client.post(f"/v1/sessions/{session_id}/events", json=_INPUT)
    assert response.status_code == 202, response.text
    [entry] = startup.conversations.list_pending_inputs(session_id)
    if revoke_access:
        startup_permissions.revoke(user_id, session_id)
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        await startup._deliver(auth_app, session_id, entry)
    finally:
        await runner.aclose()
    if revoke_access:
        assert not forwarded
        assert startup.conversations.list_pending_inputs(session_id)[0].status == "failed"
    else:
        turns = [event for event in forwarded if event.get("type") == "message"]
        assert len(turns) == 1 and _TEXT in json.dumps(turns[0])
        assert startup.conversations.list_pending_inputs(session_id) == []
        history = (await auth_client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
        [message] = [item for item in history if item["type"] == "message"]
        assert message.get("created_by") == (None if user_id == "local" else user_id)
    assert entry.created_by == user_id


@pytest.mark.parametrize("with_first", [True, False])
async def test_accepts_followups_before_provisioning_and_delivers_in_order(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch, with_first: bool
) -> None:
    env = startup_env
    session_id = await _create(env, initial_input=_INPUT if with_first else None)
    texts = [_TEXT] if with_first else []
    for text in ("Second startup instruction", "Third startup instruction"):
        event = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
                "stable_id": uuid4().hex,
            },
        }
        response = await env.client.post(f"/v1/sessions/{session_id}/events", json=event)
        assert response.status_code == 202, response.text
        assert response.json()["pending_id"]
        retry = await env.client.post(f"/v1/sessions/{session_id}/events", json=event)
        assert retry.json() == response.json()
        texts.append(text)
    snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
    assert snapshot["host_id"] is None and snapshot["runner_id"] is None
    assert [item["content"][-1]["text"] for item in snapshot["pending_inputs"]] == texts
    _bind(env, session_id)
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        for item in env.conv_store.list_pending_inputs(session_id):
            await env.app.state.startup_inputs._deliver(env.app, session_id, item)
    finally:
        await runner.aclose()
    turns = [event for event in forwarded if event.get("type") == "message"]
    assert [turn["content"][-1]["text"] for turn in turns] == texts
    assert env.conv_store.list_pending_inputs(session_id) == []


async def test_native_mirror_retry_does_not_consume_next_identical_input(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    session_id = await _create(env, name=CODEX_NATIVE_CODING_AGENT.agent_name)
    response = await env.client.post(f"/v1/sessions/{session_id}/events", json=_INPUT)
    assert response.status_code == 202, response.text
    first = _bind(env, session_id)
    startup = env.app.state.startup_inputs
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)

    async def echo(source: str):
        result = await env.client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": _INPUT["data"],
                    "response_id": source,
                    "source_id": source,
                },
            },
        )
        assert result.status_code == 202, result.text

    try:
        await startup._deliver(env.app, session_id, first)
        pending_inputs.reset_for_tests()
        await echo("native-first")
        [second] = env.conv_store.list_pending_inputs(session_id)
        await startup._deliver(env.app, session_id, second)
        await echo("native-first")
        assert env.conv_store.list_pending_inputs(session_id)[0].id == second.id
        pending_inputs.reset_for_tests()
        await echo("native-second")
    finally:
        await runner.aclose()
    assert env.conv_store.list_pending_inputs(session_id) == []
    assert len([event for event in forwarded if event.get("type") == "message"]) == 2
    snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
    assert len([item for item in snapshot["items"] if item["type"] == "message"]) == 2


async def test_pending_input_retains_request_options_on_service_restart(startup_env: _Env) -> None:
    env = startup_env
    event = {**_INPUT, "model_override": "chosen-model", "tools": []}
    session_id = await _create(env, initial_input=event)
    item = _bind(env, session_id)
    startup = ManagedStartupInputs(env.conv_store, env.app.state.host_registry)
    dispatched = []

    async def dispatch(context: StartupInputContext, session_id: str, event: SessionEventInput):
        assert await context.claim(env.conv_store, session_id)
        dispatched.append(event)
        return {"queued": True}

    startup.dispatch = dispatch
    await startup._deliver(env.app, session_id, item)
    assert dispatched[0].model_override == "chosen-model"
    assert dispatched[0].tools == []
    assert "startup_options" not in dispatched[0].data


@pytest.mark.parametrize("detail", [None, "harness is not configured on this host"])
async def test_host_refusal_keeps_startup_input_without_repeated_launches(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch, detail: str | None
) -> None:
    env = startup_env
    session_id = await _create(env)
    saved = _bind(env, session_id)
    conv = env.conv_store.get_conversation(session_id)
    env.app.state.host_registry.register(
        conv.host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="startup-test", runners=[]),
        owner=None,
        registered_with_managed_token=True,
    )
    launch = AsyncMock(
        return_value=_HostLaunchAttempt(
            runner_id=uuid4().hex, error_code="harness_not_configured", error=detail
        )
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._launch_runner_on_host", launch)
    startup = env.app.state.startup_inputs
    await startup._deliver(env.app, session_id, saved)
    await startup._deliver(env.app, session_id, saved)
    assert launch.await_count == 1
    [pending] = env.conv_store.list_pending_inputs(session_id)
    assert pending.id == saved.id and pending.status == "failed"
    [error] = env.conv_store.list_items(session_id).data
    assert error.data.code == "startup_input_failed"
    assert (detail or "The host refused to launch the session") in error.data.message


async def test_saved_sdk_skill_resolves_after_launch_and_replaces_pending_bubble(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    calls: list[tuple[str, dict[str, Any]]] = []
    meta_text = "<skill>Inspect the saved instruction and its workspace.</skill>"

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"meta_text": meta_text})
        return httpx.Response(202, json={"queued": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:

        async def get_runner(*_: Any, **__: Any) -> httpx.AsyncClient:
            return runner

        monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", get_runner)
        agent = await create_test_agent(
            env.client,
            skills=[{"name": "review", "description": "Review a change", "content": meta_text}],
        )
        response = await env.client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_type": "managed",
                "initial_input": {
                    "type": "slash_command",
                    "data": {
                        "kind": "skill",
                        "name": "review",
                        "arguments": "the startup flow",
                    },
                },
            },
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        saved = _bind(env, session_id)
        assert not [path for path, _ in calls if path.endswith(("/skills/resolve", "/events"))], (
            "skill resolution and delivery must wait for the host"
        )
        startup = env.app.state.startup_inputs
        await startup._deliver(env.app, session_id, saved)
        await startup._deliver(env.app, session_id, saved)

    assert len([path for path, _ in calls if path.endswith("/skills/resolve")]) == 1
    [turn] = [body for path, body in calls if path.endswith("/events")]
    assert turn["content"] == [{"type": "input_text", "text": meta_text}]
    assert env.conv_store.list_pending_inputs(session_id) == []
    items = env.conv_store.list_items(session_id).data
    [visible] = [item for item in items if item.type == "slash_command"]
    [hidden] = [item for item in items if item.type == "message"]
    assert visible.id == saved.id
    assert visible.data.name == "review" and visible.data.arguments == "the startup flow"
    assert hidden.data.is_meta and hidden.id == turn["persisted_item_id"]


def _connect_host(env: _Env, host_id: str, *, managed: bool = True) -> Any:
    return env.app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="startup-test", runners=[]),
        owner=None,
        registered_with_managed_token=managed,
    )


def _stub_launch(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_code: str | None = None,
    error: str | None = None,
) -> AsyncMock:
    """Stand in for the host's spawn verdict; the real launch binds the runner first."""

    async def launch(conv: Any, *_: Any) -> _HostLaunchAttempt:
        runner_id = uuid4().hex
        env.conv_store.set_runner_id(conv.id, runner_id)
        return _HostLaunchAttempt(runner_id=runner_id, error_code=error_code, error=error)

    stub = AsyncMock(side_effect=launch)
    monkeypatch.setattr("omnigent.server.routes.sessions._launch_runner_on_host", stub)
    return stub


async def test_host_replica_starts_the_runner_its_launch_skipped(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that bound the host on another replica left no runner; the host's replica
    starts one, then clears the session's startup state."""
    env = startup_env
    session_id = await _create(env, initial_input=None)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
    connection = _connect_host(env, host_id)
    launch = _stub_launch(env, monkeypatch)
    startup = env.app.state.startup_inputs

    assert await startup._recover_session(env.app, session_id, connection) is True
    assert launch.await_count == 1
    assert await startup._recover_session(env.app, session_id, connection) is False
    assert launch.await_count == 1
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and STARTUP_LABEL not in conv.labels
    assert env.conv_store.list_items(session_id).data == []


async def test_refused_recovery_launch_is_reported_once(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    session_id = await _create(env, initial_input=None)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
    connection = _connect_host(env, host_id)
    launch = _stub_launch(
        env,
        monkeypatch,
        error_code="harness_not_configured",
        error="harness is not configured on this host",
    )
    startup = env.app.state.startup_inputs

    await startup._recover_session(env.app, session_id, connection)
    await startup._recover_session(env.app, session_id, connection)
    assert launch.await_count == 1
    [error] = env.conv_store.list_items(session_id).data
    assert isinstance(error.data, ErrorData) and error.data.code == "startup_input_failed"
    assert "harness is not configured on this host" in error.data.message


async def test_finished_startup_skips_pending_input_reads(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    session_id = await _create(env, initial_input=None)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
    env.conv_store.set_runner_id(session_id, uuid4().hex)
    startup = env.app.state.startup_inputs
    assert (
        await startup._recover_session(env.app, session_id, _connect_host(env, host_id)) is False
    )
    reads = AsyncMock()
    monkeypatch.setattr(env.conv_store, "list_pending_inputs", reads)
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert not await startup.accepts(conv, env.app)
    reads.assert_not_called()


async def test_host_worker_starts_the_runner_once_the_launch_binds_its_host(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host connects before its launch binds a session; the worker keeps checking."""
    env = startup_env
    session_id = await _create(env, initial_input=None)
    host_id = uuid4().hex
    _connect_host(env, host_id)
    launch = _stub_launch(env, monkeypatch)
    startup = env.app.state.startup_inputs

    startup.host_changed(env.app, host_id)
    [(_, worker, _)] = startup._workers.values()
    try:
        await asyncio.sleep(0.5)
        assert launch.await_count == 0
        env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
        for _ in range(100):
            conv = env.conv_store.get_conversation(session_id)
            if conv is not None and STARTUP_LABEL not in conv.labels:
                break
            await asyncio.sleep(0.1)
        assert launch.await_count == 1
        assert conv is not None and STARTUP_LABEL not in conv.labels
        assert not worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_input_saved_while_startup_finishes_is_still_delivered(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follow-up accepted just before the label was cleared must not be stranded."""
    env = startup_env
    session_id = await _create(env, initial_input=None)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
    env.conv_store.set_runner_id(session_id, uuid4().hex)
    connection = _connect_host(env, host_id)
    startup = env.app.state.startup_inputs
    assert await startup._recover_session(env.app, session_id, connection) is False
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and STARTUP_LABEL not in conv.labels

    saved = await startup.enqueue(
        session_id,
        SessionEventInput.model_validate(_INPUT),
        user_id=None,
        headers=Headers(),
        first=False,
    )
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.labels[STARTUP_LABEL] == "ready"
    delivered: list[str] = []

    async def deliver(_app: Any, _session_id: str, item: Any) -> None:
        delivered.append(item.id)

    monkeypatch.setattr(startup, "_deliver", deliver)
    env.conv_store.delete_label(session_id, STARTUP_LABEL)
    assert await startup._recover_session(env.app, session_id, connection) is True
    await asyncio.sleep(0)
    assert delivered == [saved.id]


async def test_failed_create_launch_is_visible_to_every_replica(
    startup_env: _Env,
) -> None:
    env = startup_env
    session_id = await _create(env)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
    startup = env.app.state.startup_inputs
    await startup.record_launch_failure(session_id, "spend limit reached")
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.labels[STARTUP_LABEL] == "failed"
    # No sandbox is left to deliver the saved first input; say so in the transcript.
    [pending] = env.conv_store.list_pending_inputs(session_id)
    assert pending.status == "failed"
    [error] = env.conv_store.list_items(session_id).data
    assert isinstance(error.data, ErrorData) and "spend limit reached" in error.data.message
    # Another replica has no tracker entry for the launch.
    env.app.state.managed_launches = type(env.app.state.managed_launches)()
    assert not await startup.accepts(conv, env.app)
    assert (
        await startup._recover_session(env.app, session_id, _connect_host(env, host_id)) is False
    )


async def _session_count(env: _Env) -> int:
    response = await env.client.get("/v1/sessions")
    assert response.status_code == 200, response.text
    return len(response.json()["data"])


async def test_initial_files_without_initial_input_create_nothing(startup_env: _Env) -> None:
    env = startup_env
    agent = await create_test_agent(env.client)
    before = await _session_count(env)
    response = await env.client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"agent_id": agent["id"], "host_type": "managed"})},
        files=[("initial_files", ("context.txt", b"orphan attachment", "text/plain"))],
    )
    assert response.status_code == 400, response.text
    assert await _session_count(env) == before


async def test_failed_initial_upload_deletes_the_new_session(
    startup_env: _Env, db_uri: str
) -> None:
    env = startup_env
    agent = await create_test_agent(env.client)
    before = await _session_count(env)
    created: list[str] = []
    upload = env.app.state.initial_file_upload

    async def failing_upload(request: Any, session_id: str, file: Any) -> dict[str, Any]:
        created.append(session_id)
        await upload(request, session_id, file)
        raise RuntimeError("attachment storage is unavailable")

    env.app.state.initial_file_upload = failing_upload
    with pytest.raises(RuntimeError, match="attachment storage is unavailable"):
        await env.client.post(
            "/v1/sessions",
            data={
                "metadata": json.dumps(
                    {"agent_id": agent["id"], "host_type": "managed", "initial_input": _INPUT}
                )
            },
            files=[("initial_files", ("context.txt", b"never delivered", "text/plain"))],
        )
    assert await _session_count(env) == before
    [session_id] = created
    assert env.conv_store.get_conversation(session_id) is None
    assert SqlAlchemyFileStore(db_uri).list(session_id=session_id).data == []


async def test_crashed_create_launch_is_recorded_on_the_session(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env

    async def crash(**_: Any) -> None:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(routes_core, "_run_managed_launch", crash)
    session_id = await _create(env)
    # The failed label lands before its error item.
    for _ in range(50):
        conv = env.conv_store.get_conversation(session_id)
        items = env.conv_store.list_items(session_id).data
        if conv is not None and conv.labels.get(STARTUP_LABEL) == "failed" and items:
            break
        await asyncio.sleep(0.1)
    assert conv is not None and conv.labels.get(STARTUP_LABEL) == "failed"
    [error] = items
    assert isinstance(error.data, ErrorData) and "store unavailable" in error.data.message


async def test_launch_cancelled_at_shutdown_before_binding_fails_its_saved_input(
    startup_env: _Env,
) -> None:
    """A launch shutdown gives up on before it binds a host leaves no replica to deliver
    the saved first message, so the session reports that instead of waiting forever."""
    from omnigent.server.routes.sessions import cancel_managed_launch_tasks

    env = startup_env
    session_id = await _create(env)

    await asyncio.wait_for(cancel_managed_launch_tasks(drain_s=0.2), timeout=5)

    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.labels[STARTUP_LABEL] == "failed"
    [pending] = env.conv_store.list_pending_inputs(session_id)
    assert pending.status == "failed"
    [error] = env.conv_store.list_items(session_id).data
    assert isinstance(error.data, ErrorData) and "shut down" in error.data.message


async def test_refused_input_does_not_hold_back_the_next_one(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Policy denies A; B behind it is still delivered, and retrying A doesn't replay B."""
    env = startup_env
    session_id = await _create(env)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/managed-workspace")
    env.conv_store.set_runner_id(session_id, uuid4().hex)
    connection = _connect_host(env, host_id)
    startup = env.app.state.startup_inputs
    first = env.conv_store.list_pending_inputs(session_id)[0]
    follow_up = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "Second input"}]},
    }
    second = await startup.enqueue(
        session_id,
        SessionEventInput.model_validate(follow_up),
        user_id=None,
        headers=Headers(),
        first=False,
    )
    denied_texts = {_TEXT}

    async def evaluate(_request: Any, _session_id: str, _conv: Any, body: Any, *_: Any, **__: Any):
        text = body.data["content"][-1]["text"]
        return (
            {"verdict": "deny", "reason": "Blocked test input"} if text in denied_texts else None
        )

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_events._evaluate_input_policy", evaluate
    )
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)

    def sent() -> list[str]:
        return [
            event["content"][-1]["text"] for event in forwarded if event.get("type") == "message"
        ]

    async def recover_until_idle() -> None:
        for _ in range(10):
            await startup._recover_session(env.app, session_id, connection)
            await asyncio.gather(*startup._deliveries.values(), return_exceptions=True)

    try:
        await recover_until_idle()
        assert sent() == ["Second input"]
        [refused] = env.conv_store.list_pending_inputs(session_id)
        assert refused.id == first.id and refused.status == "failed"
        assert second.id not in {
            item.id for item in env.conv_store.list_pending_inputs(session_id)
        }

        denied_texts.clear()
        assert await startup.retry(session_id)
        await recover_until_idle()
        assert sent() == ["Second input", _TEXT]
        assert env.conv_store.list_pending_inputs(session_id) == []
    finally:
        await runner.aclose()


async def test_native_receipt_wakes_the_host_worker(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript echo that replaces a submitted input frees the next one at once."""
    env = startup_env
    session_id = await _create(env, name=CODEX_NATIVE_CODING_AGENT.agent_name)
    entry = _bind(env, session_id)
    startup = env.app.state.startup_inputs

    async def dispatch(context: StartupInputContext, session_id: str, _: SessionEventInput):
        assert await context.claim(env.conv_store, session_id)
        return {"queued": True, "pending_id": "pending"}

    startup.dispatch = dispatch
    await startup._deliver(env.app, session_id, entry)
    assert env.conv_store.list_pending_inputs(session_id)[0].status == "incomplete"
    woken: list[str] = []
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.wake_startup_worker", woken.append
    )
    pending_inputs.reset_for_tests()
    await _simulate_transcript_forwarder_echo(env.client, session_id, _TEXT)
    assert env.conv_store.list_pending_inputs(session_id) == []
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and woken == [conv.host_id]


async def test_waking_a_host_worker_sets_its_event(startup_env: _Env) -> None:
    env = startup_env
    host_id = uuid4().hex
    _connect_host(env, host_id)
    startup = env.app.state.startup_inputs
    startup.host_changed(env.app, host_id)
    [(_, worker, wake)] = startup._workers.values()
    try:
        wake.clear()
        wake_startup_worker(host_id)
        assert wake.is_set()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize(
    "failed_stage",
    [
        "runner_start",
        "input_claim",
        "prompt_send",
        "dispatch_bookkeeping",
        "post_delivery",
        "post_delivery_cancelled",
    ],
)
async def test_saved_delivery_identifies_failed_boundary(
    startup_env, monkeypatch, scheduled_run_context, caplog, failed_stage
):
    caplog.set_level(logging.INFO)
    cancelled = failed_stage == "post_delivery_cancelled"
    reported_stage = failed_stage.removesuffix("_cancelled")
    env = startup_env
    with scheduled_run_scope(scheduled_run_context):
        session_id = await _create(env)
    entry = _bind(env, session_id)
    forwarded = []
    runner = _stub_runner(
        monkeypatch, forwarded, status=503 if failed_stage == "prompt_send" else 202
    )
    if failed_stage == "runner_start":
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client",
            AsyncMock(side_effect=ConnectionError("runner unavailable")),
        )
    elif failed_stage == "input_claim":
        original = env.conv_store.transition_pending_input

        def fail_claim(*args, **kwargs):
            if kwargs.get("status") == "incomplete":
                raise RuntimeError("claim unavailable")
            return original(*args, **kwargs)

        monkeypatch.setattr(env.conv_store, "transition_pending_input", fail_claim)
    elif failed_stage == "dispatch_bookkeeping":
        from omnigent.server.routes._sessions import orchestration

        monkeypatch.setattr(
            orchestration,
            "_publish_input_consumed",
            Mock(side_effect=RuntimeError("bookkeeping failed after acceptance")),
        )
    elif reported_stage == "post_delivery":
        monkeypatch.setattr(
            env.conv_store,
            "set_labels",
            Mock(
                side_effect=(
                    asyncio.CancelledError("bookkeeping cancelled")
                    if cancelled
                    else RuntimeError("bookkeeping failed")
                )
            ),
        )
    try:
        startup = env.app.state.startup_inputs
        if cancelled:
            with pytest.raises(asyncio.CancelledError, match="bookkeeping cancelled"):
                await startup._deliver(env.app, session_id, entry)
            assert env.conv_store.list_pending_inputs(session_id) == []
            assert not await startup.retry(session_id)
            await startup._deliver(env.app, session_id, entry)
            assert len([event for event in forwarded if event.get("type") == "message"]) == 1
        else:
            await startup._deliver(env.app, session_id, entry)
    finally:
        await runner.aclose()
    failure_event = (
        "scheduled_run_prompt_dispatch_interrupted" if cancelled else "scheduled_run_stage_failed"
    )
    failures = [r for r in caplog.records if getattr(r, "event_name", None) == failure_event]
    if cancelled:
        assert not any(
            getattr(r, "event_name", None) == "scheduled_run_stage_failed" for r in caplog.records
        )
    assert len(failures) == 1
    assert failures[0].attributes["failure_stage"] == (
        "prompt_send" if failed_stage == "dispatch_bookkeeping" else reported_stage
    )
    if failed_stage == "dispatch_bookkeeping":
        assert len([event for event in forwarded if event.get("type") == "message"]) == 1
    assert failures[0].attributes["outcome"] == (
        "sent"
        if reported_stage == "post_delivery"
        else "uncertain"
        if failed_stage in {"prompt_send", "dispatch_bookkeeping"}
        else "not_sent"
    )
    assert len(
        [
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "scheduled_run_prompt_sent"
        ]
    ) == int(reported_stage == "post_delivery")


@pytest.mark.parametrize("mode", ["managed", "external", "shared_sdk"])
async def test_edited_fork_saves_revised_input_and_original_attachment_before_launch(
    startup_env: _Env, db_uri: str, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env = startup_env
    agent = await create_test_agent(env.client)
    source_response = await env.client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert source_response.status_code == 201, source_response.text
    source_id = source_response.json()["id"]
    upload = await env.client.post(
        f"/v1/sessions/{source_id}/resources/files",
        files={"file": ("context.txt", b"original attachment", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    original_file_id = upload.json()["id"]
    original_content = [
        {"type": "input_file", "file_id": original_file_id, "filename": "context.txt"},
        {"type": "input_text", "text": "original question"},
    ]
    source_items = env.conv_store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="earlier",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "earlier question"}]
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="earlier",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": "earlier answer"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="original",
                data=MessageData(role="user", content=original_content),
            ),
            NewConversationItem(
                type="message",
                response_id="original",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": "stale answer"}],
                ),
            ),
        ],
    )
    if mode == "external":
        env.conv_store.set_host_id(source_id, uuid4().hex, "/tmp/source-workspace")
    elif mode == "shared_sdk":
        env.conv_store.set_runner_id(source_id, "shared-sdk-runner")
        monkeypatch.setattr(env.app.state.runner_router, "runner_is_online", lambda _: True)
    original_snapshot = env.conv_store.list_items(source_id).data
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        response = await env.client.post(
            f"/v1/sessions/{source_id}/fork",
            json={
                "before_item_id": source_items[2].id,
                "edited_text": "revised question with more context",
                "interrupt_source": True,
                **({"host_type": "managed"} if mode == "managed" else {}),
            },
        )
        assert response.status_code == 201, response.text
        fork_id = response.json()["id"]
        assert env.conv_store.list_items(source_id).data == original_snapshot
        snapshot = (await env.client.get(f"/v1/sessions/{fork_id}")).json()
        if mode == "shared_sdk":
            assert snapshot["pending_inputs"] == []
            assert STARTUP_LABEL not in env.conv_store.get_conversation(fork_id).labels
        else:
            [queued] = snapshot["pending_inputs"]
            assert queued["content"][1]["text"] == "revised question with more context"
            copied_file_id = queued["content"][0]["file_id"]
            assert copied_file_id != original_file_id
            assert SqlAlchemyFileStore(db_uri).get(copied_file_id).session_id == fork_id
            assert not [event for event in forwarded if event.get("type") == "message"]
            original_startup = env.app.state.startup_inputs
            saved = _bind(env, fork_id)
            restarted = ManagedStartupInputs(env.conv_store, original_startup.hosts)
            restarted.dispatch = original_startup.dispatch
            host_id = env.conv_store.get_conversation(fork_id).host_id
            _connect_host(env, host_id, managed=mode == "managed")
            restarted.host_changed(env.app, host_id)
            try:
                for _ in range(100):
                    if not env.conv_store.list_pending_inputs(fork_id) and any(
                        event.get("type") == "message" for event in forwarded
                    ):
                        break
                    await asyncio.sleep(0.02)
                await restarted._deliver(env.app, fork_id, saved)
            finally:
                await restarted.shutdown()
        turns = [event for event in forwarded if event.get("type") == "message"]
        assert len(turns) == 1
        assert turns[0]["content"][1]["text"] == "revised question with more context"
        assert base64.b64decode(turns[0]["content"][0]["file_data"].split(",", 1)[1]) == (
            b"original attachment"
        )
        snapshot = (await env.client.get(f"/v1/sessions/{fork_id}")).json()
        assert "original question" not in json.dumps(snapshot["items"])
        assert "stale answer" not in json.dumps(snapshot["items"])
        assert "earlier question" in json.dumps(snapshot["items"])
        if mode == "shared_sdk":
            subsequent = await env.client.post(f"/v1/sessions/{fork_id}/events", json=_INPUT)
            assert subsequent.status_code == 202, subsequent.text
            assert len([event for event in forwarded if event.get("type") == "message"]) == 2
    finally:
        await runner.aclose()


async def test_edited_fork_does_not_create_branch_when_interrupt_fails(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    source_id = await _create(env)
    # Use a committed user message rather than the startup queue.
    [source_item] = env.conv_store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="source",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "original"}]
                ),
            )
        ],
    )
    before = await _session_count(env)
    original_dispatch = env.app.state.startup_inputs.dispatch

    async def refuse(*_: Any) -> dict[str, bool]:
        raise RuntimeError("interrupt failed")

    env.app.state.startup_inputs.dispatch = refuse
    try:
        with pytest.raises(RuntimeError, match="interrupt failed"):
            await env.client.post(
                f"/v1/sessions/{source_id}/fork",
                json={
                    "before_item_id": source_item.id,
                    "edited_text": "revised",
                    "interrupt_source": True,
                },
            )
    finally:
        env.app.state.startup_inputs.dispatch = original_dispatch
    assert await _session_count(env) == before


async def test_read_only_user_cannot_edit_or_interrupt_source(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    startup_permissions: SqlAlchemyPermissionStore,
) -> None:
    auth_client.headers["X-Forwarded-Email"] = "alice"
    agent = await create_test_agent(auth_client)
    response = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code == 201, response.text
    source_id = response.json()["id"]
    startup = auth_app.state.startup_inputs
    [original] = startup.conversations.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="source",
                created_by="alice",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "original"}]
                ),
            )
        ],
    )
    startup_permissions.ensure_user("reader")
    startup_permissions.grant("reader", source_id, LEVEL_READ)
    dispatch = startup.dispatch
    interrupted = AsyncMock()
    startup.dispatch = interrupted
    try:
        response = await auth_client.post(
            f"/v1/sessions/{source_id}/fork",
            json={
                "before_item_id": original.id,
                "edited_text": "revised",
                "interrupt_source": True,
            },
            headers={"X-Forwarded-Email": "reader"},
        )
    finally:
        startup.dispatch = dispatch
    assert response.status_code == 403, response.text
    interrupted.assert_not_awaited()
    assert startup.conversations.list_items(source_id).data == [original]


@pytest.mark.parametrize("failure", ["offline", "refused", "disconnected"])
@pytest.mark.parametrize("source_status", ["running", "idle", "failed"])
async def test_edited_fork_requires_active_source_interrupt_acknowledgment(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch, failure: str, source_status: str
) -> None:
    env = startup_env
    source_id = await _create(env, initial_input=None)
    [original] = env.conv_store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="source",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "original"}]
                ),
            )
        ],
    )
    env.conv_store.set_host_id(source_id, uuid4().hex, "/tmp/source-workspace")
    env.conv_store.set_session_live_status(source_id, source_status)
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded, status=503)

    async def unavailable(*_: Any, **__: Any):
        if failure == "disconnected":
            raise ConnectionError("runner tunnel disconnected")
        return

    if failure == "offline":
        monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", unavailable)
    elif failure == "disconnected":
        monkeypatch.setattr(runner, "post", unavailable)
    before = await _session_count(env)
    try:
        response = await env.client.post(
            f"/v1/sessions/{source_id}/fork",
            json={
                "before_item_id": original.id,
                "edited_text": "revised",
                "interrupt_source": True,
            },
        )
    finally:
        await runner.aclose()
    if source_status == "running":
        assert response.status_code == 503, response.text
        assert "interrupt" in response.text
        assert await _session_count(env) == before
    else:
        assert response.status_code == 201, response.text
        assert await _session_count(env) == before + 1
        assert not forwarded
    assert env.conv_store.get_conversation(source_id).live_status == source_status


@pytest.mark.parametrize("resume", ["retry", "followup"])
async def test_hostless_edited_input_recovers_after_restart_before_dispatch(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch, resume: str
) -> None:
    env = startup_env
    agent = await create_test_agent(env.client)
    response = await env.client.post("/v1/sessions", json={"agent_id": agent["id"]})
    source_id = response.json()["id"]
    [original] = env.conv_store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="source",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "original"}]
                ),
            )
        ],
    )
    env.conv_store.set_runner_id(source_id, "shared-sdk-runner")
    monkeypatch.setattr(env.app.state.runner_router, "runner_is_online", lambda _: True)
    original_startup = env.app.state.startup_inputs
    monkeypatch.setattr(original_startup, "dispatch_pending", AsyncMock())
    response = await env.client.post(
        f"/v1/sessions/{source_id}/fork",
        json={"before_item_id": original.id, "edited_text": "revised"},
    )
    assert response.status_code == 201, response.text
    fork_id = response.json()["id"]
    assert env.conv_store.get_conversation(fork_id).runner_id == "shared-sdk-runner"
    restarted = ManagedStartupInputs(env.conv_store, original_startup.hosts)
    restarted.dispatch = original_startup.dispatch
    env.app.state.startup_inputs = restarted
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        response = await env.client.post(
            f"/v1/sessions/{fork_id}/events",
            json=({"type": "retry_session", "data": {}} if resume == "retry" else _INPUT),
        )
        assert response.status_code == 202, response.text
        turns = [event for event in forwarded if event.get("type") == "message"]
        assert len(turns) == (1 if resume == "retry" else 2)
        assert turns[0]["content"][0]["text"] == "revised"
        assert env.conv_store.list_pending_inputs(fork_id) == []
        assert STARTUP_LABEL not in env.conv_store.get_conversation(fork_id).labels
    finally:
        env.app.state.startup_inputs = original_startup
        await restarted.shutdown()
        await runner.aclose()


async def test_edited_fork_cannot_reuse_another_users_runner(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    startup_permissions: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_client.headers["X-Forwarded-Email"] = "alice"
    agent = await create_test_agent(auth_client)
    response = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]})
    source_id = response.json()["id"]
    startup = auth_app.state.startup_inputs
    [original] = startup.conversations.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="source",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "original"}]
                ),
            )
        ],
    )
    startup.conversations.set_runner_id(source_id, "alices-runner")
    startup_permissions.ensure_user("editor")
    startup_permissions.grant("editor", source_id, LEVEL_EDIT)
    monkeypatch.setattr(auth_app.state.runner_router, "runner_is_online", lambda _: True)
    monkeypatch.setattr(auth_app.state.runner_router, "runner_owner", lambda _: "alice")
    interrupted = AsyncMock()
    monkeypatch.setattr(startup, "dispatch", interrupted)
    response = await auth_client.post(
        f"/v1/sessions/{source_id}/fork",
        json={"before_item_id": original.id, "edited_text": "revised", "interrupt_source": True},
        headers={"X-Forwarded-Email": "editor"},
    )
    assert response.status_code == 403, response.text
    interrupted.assert_not_awaited()


@pytest.mark.parametrize("has_earlier_history", [False, True])
async def test_edited_fork_rejects_lost_context_on_non_replaying_native_harness(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch, has_earlier_history: bool
) -> None:
    env = startup_env
    source_id = await _create(env, initial_input=None)
    items = env.conv_store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="source",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for text in (["earlier", "original"] if has_earlier_history else ["original"])
        ],
    )
    monkeypatch.setattr(
        routes_core, "_native_coding_agent_for_agent", lambda _: KIRO_NATIVE_CODING_AGENT
    )
    response = await env.client.post(
        f"/v1/sessions/{source_id}/fork",
        json={"before_item_id": items[-1].id, "edited_text": "revised"},
    )
    assert response.status_code == (400 if has_earlier_history else 201), response.text
    if has_earlier_history:
        assert "cannot replay earlier conversation history" in response.text


async def test_managed_host_recovers_saved_input_without_startup_label(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    source_id = await _create(env)
    _bind(env, source_id)
    env.conv_store.delete_label(source_id, STARTUP_LABEL)
    host_id = env.conv_store.get_conversation(source_id).host_id
    _connect_host(env, host_id)
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        env.app.state.startup_inputs.host_changed(env.app, host_id)
        for _ in range(100):
            if not env.conv_store.list_pending_inputs(source_id) and any(
                event.get("type") == "message" for event in forwarded
            ):
                break
            await asyncio.sleep(0.02)
        assert env.conv_store.list_pending_inputs(source_id) == []
        assert len([event for event in forwarded if event.get("type") == "message"]) == 1
    finally:
        await env.app.state.startup_inputs.shutdown()
        await runner.aclose()


@pytest.mark.parametrize("before_item_id", ["not-an-id", "f" * 32])
async def test_edited_fork_rejects_malformed_or_missing_item_on_real_store(
    startup_env: _Env, before_item_id: str
) -> None:
    env = startup_env
    source_id = await _create(env, initial_input=None)
    before = await _session_count(env)
    response = await env.client.post(
        f"/v1/sessions/{source_id}/fork",
        json={
            "before_item_id": before_item_id,
            "edited_text": "revised",
            "interrupt_source": True,
        },
    )
    assert response.status_code == 400, response.text
    assert await _session_count(env) == before
