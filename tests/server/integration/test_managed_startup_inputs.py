"""Accepted managed input uses normal dispatch after the creating request is gone."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.datastructures import Headers

from omnigent.entities.conversation import ErrorData
from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT, CODEX_NATIVE_CODING_AGENT
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime import pending_inputs
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.server.routes._sessions.helpers import _HostLaunchAttempt
from omnigent.server.routes.sessions import routes_core
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
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """The shared auth-enabled app, with managed sandboxes configured."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
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
async def test_saved_input_survives_service_recreation_and_dispatches_once(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch, name: str, native: bool
) -> None:
    env = startup_env
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        session_id = await _create(env, name=name)
        original = env.app.state.startup_inputs
        entry = env.conv_store.list_pending_inputs(session_id)[0]
        assert entry.status == "in_progress"
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
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
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


async def test_stop_cancels_local_dispatch_and_clears_saved_input(startup_env: _Env) -> None:
    env = startup_env
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


async def test_uncertain_native_submission_is_not_retried_on_restart(startup_env: _Env) -> None:
    env = startup_env
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
    entered.clear()
    await startup._deliver(env.app, session_id, entry)
    assert not entered.is_set()
    assert not await startup.retry(session_id)
    pending_inputs.reset_for_tests()
    await _simulate_transcript_forwarder_echo(env.client, session_id, _TEXT)
    assert env.conv_store.list_pending_inputs(session_id) == []


async def test_dispatch_rechecks_saved_creators_access(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await create_test_agent(auth_client, user="alice")
    response = await auth_client.post(
        "/v1/sessions", json={"agent_id": agent["id"]}, headers={"X-Forwarded-Email": "alice"}
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    startup = auth_app.state.startup_inputs
    entry = await startup.enqueue(
        session_id, SessionEventInput.model_validate(_INPUT), user_id="alice", headers=Headers()
    )
    SqlAlchemyPermissionStore(db_uri).revoke("alice", session_id)
    forwarded: list[dict[str, Any]] = []
    runner = _stub_runner(monkeypatch, forwarded)
    try:
        await startup._deliver(auth_app, session_id, entry)
    finally:
        await runner.aclose()
    assert not forwarded
    assert startup.conversations.list_pending_inputs(session_id)[0].status == "failed"


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


def _connect_host(env: _Env, host_id: str) -> Any:
    return env.app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="startup-test", runners=[]),
        owner=None,
        registered_with_managed_token=True,
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
