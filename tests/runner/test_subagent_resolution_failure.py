"""An unresolved child must fail before dispatch and remain retryable after repair."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeMcpManager
from tests.runner.test_runner_dispatch import (
    _CONTRACT_ADAPTERS,
    _INSTRUCTION_WARN_CHUNKS,
    _await_bg_turn_task,
    _contract_root_spec,
    _contract_run_background,
    _ContractSnapshotClient,
    _FakeProcessManager,
    _RecordingHarnessClient,
    _runner_test_client,
)


class _RecordingManager(_FakeProcessManager):
    def __init__(self, harness: Any) -> None:
        super().__init__(harness)
        self.spawns: list[tuple[str, str]] = []

    async def get_client(
        self, conversation_id: str, harness_name: str, *, env: dict[str, str] | None = None
    ) -> Any:
        self.spawns.append((conversation_id, harness_name))
        return await super().get_client(conversation_id, harness_name, env=env)


def _statuses(app: Any, session_id: str) -> list[dict[str, Any]]:
    queue = app.state.session_event_queues[session_id]
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.get("type") == "session.status":
            events.append(event)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id_in_body", [False, True])
@pytest.mark.parametrize("snapshot_failure", [None, 503, "timeout"])
@pytest.mark.parametrize("path", ["background", "known_harness", "no_harness"])
async def test_cold_child_resolution_survives_multiple_turns(
    agent_id_in_body: bool,
    snapshot_failure: int | str | None,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached child is already selected, including when create was on another runner."""
    conv = "conv_cold_child"
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    calls = []

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        calls.append(agent_id)
        return _contract_root_spec(with_child=True)

    server = _ContractSnapshotClient(conv)
    original_get = server.get
    outage_active = bool(snapshot_failure)

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{conv}") and outage_active:
            if snapshot_failure == "timeout":
                raise httpx.ReadTimeout("metadata unavailable")
            return httpx.Response(snapshot_failure)
        return await original_get(url, **kwargs)

    monkeypatch.setattr(server, "get", get)
    manager = _RecordingManager(recording)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=server,  # type: ignore[arg-type]
    )
    body: dict[str, Any] = {"type": "message", "role": "user", "content": "hi"}
    if agent_id_in_body:
        body["agent_id"] = "ag_contract_root"
    if path == "known_harness":
        body["harness"] = "hermes"
    url = f"/v1/sessions/{conv}/events"
    if path != "background":
        url += "?stream=true"
    async with _runner_test_client(app) as http:
        if snapshot_failure:
            response = await http.post(url, json=body)
            assert response.status_code == (202 if path == "background" else 500)
            await _await_bg_turn_task(conv)
            assert _statuses(app, conv)[-1]["status"] == "failed"
            assert not manager.spawns
            assert not recording.posted_bodies
            outage_active = False
            calls.clear()
        for _ in range(2):
            response = await http.post(url, json=body)
            assert response.status_code == (202 if path == "background" else 200)
            await _await_bg_turn_task(conv)
            assert _statuses(app, conv)[-1]["status"] == "idle"
    assert len(calls) == 1
    assert len(recording.posted_bodies) == 2
    assert all("Worker instructions." in body["instructions"] for body in recording.posted_bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["background", "known_harness", "no_harness"])
async def test_renamed_child_fails_notifies_parent_and_recovers(path: str) -> None:
    """A bundle rename invalidates an old child instead of substituting its parent."""
    conv = "conv_renamed_child"
    parent = "conv_renamed_parent"
    spec = _contract_root_spec(with_child=True)
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    manager = _RecordingManager(recording)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=_ContractSnapshotClient(conv),  # type: ignore[arg-type]
    )
    try:
        async with _runner_test_client(app) as http:
            await _contract_run_background(http, conv, recording)
            assert _statuses(app, conv)[-1]["status"] == "idle"
            assert "Worker instructions." in recording.posted_bodies[-1]["instructions"]

            spec.sub_agents[0].name = "worker_renamed"
            reset = await http.post(
                f"/v1/sessions/{conv}/agent-cache/reset", json={"agent_id": "ag_contract_root"}
            )
            assert reset.status_code == 200
            manager.spawns.clear()
            recording.posted_bodies.clear()
            inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            runner_app._session_inboxes_ref[parent] = inbox
            runner_app.register_subagent_work(
                parent_session_id=parent, child_session_id=conv, agent="worker", title="identity"
            )

            result = await _CONTRACT_ADAPTERS[path](http, conv, recording)
            if path == "background":
                assert result["status"] == 202
            else:
                assert result["status"] == 410
                assert result["error"]["code"] == "sub_agent_unresolved"
                assert "worker" in result["error"]["message"]

            failure = _statuses(app, conv)[-1]
            assert failure["status"] == "failed"
            assert failure["error"]["code"] == "sub_agent_unresolved"
            completion = await asyncio.wait_for(inbox.get(), timeout=10)
            assert completion["status"] == "failed"
            assert "worker" in completion["output"]
            assert not manager.spawns
            assert not recording.posted_bodies

            # Rejected lookups must not leave the parent cached or the turn active.
            resources = await http.get(f"/v1/sessions/{conv}/resources")
            assert resources.status_code == 410
            result = await _CONTRACT_ADAPTERS["known_harness"](http, conv, recording)
            assert result["status"] == 410
            assert not manager.spawns
            assert not recording.posted_bodies

            spec.sub_agents[0].name = "worker"
            result = await _CONTRACT_ADAPTERS[path](http, conv, recording)
            assert result["status"] == (202 if path == "background" else 200)
            assert _statuses(app, conv)[-1]["status"] == "idle"
            assert "Worker instructions." in recording.posted_bodies[-1]["instructions"]
    finally:
        runner_app.unregister_subagent_work(conv)
        runner_app._session_inboxes_ref.pop(parent, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper",
    [
        "claude-code-native-ui-subagent",
        "codex-native-ui-subagent",
        "opencode-native-ui-subagent",
        "devin-native-ui-subagent",
        "antigravity-native-ui-subagent",
        "acp",
        "claude-code-native-ui",
    ],
)
async def test_native_mirror_resources_use_the_owning_parent(
    wrapper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrored display names are not bundle children; dispatched native agents are."""
    owner, mirror = "conv_owner", "conv_mirror"
    server = _ContractSnapshotClient(owner)
    original_get = server.get
    labels = (
        {"omnigent.acp.subagent_id": "native_child"}
        if wrapper == "acp"
        else {"omnigent.wrapper": wrapper}
    )
    calls = []

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(
                200,
                json={
                    "agent_id": "ag_contract_root",
                    "sub_agent_name": "Explore",
                    "parent_session_id": owner,
                    "labels": labels,
                },
            )
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        calls.append(session_id)
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        for _ in range(2):
            response = await http.get(f"/v1/sessions/{mirror}/resources")
            if wrapper == "claude-code-native-ui":
                assert response.status_code == 410
                assert response.json()["error"]["code"] == "sub_agent_unresolved"
                break
            assert response.status_code == 200, response.text
            assert calls == [owner]
            await http.post(f"/v1/sessions/{mirror}/agent-cache/reset", json={})
    assert not manager.spawns


def _mirror_snapshot(owner: str, display_name: str = "Explore") -> dict[str, Any]:
    return {
        "agent_id": "ag_contract_root",
        "sub_agent_name": display_name,
        "parent_session_id": owner,
        "labels": {"omnigent.wrapper": "claude-code-native-ui-subagent"},
    }


# A mirror's display name may coincide with a declared child (`worker`); the
# turn is refused either way.
_MIRROR_DISPLAY_NAMES = ["Explore", "worker"]


@pytest.mark.asyncio
async def test_missing_child_mcp_requests_return_json_rpc_errors() -> None:
    """MCP requests for a missing child keep their JSON-RPC error response."""
    conv = "conv_missing_child_mcp"
    mcp = _FakeMcpManager(tool_name="jira__search_issues")
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=False)

    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        mcp_manager=mcp,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=_ContractSnapshotClient(conv),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        for method in ("tools/list", "tools/call"):
            response = await http.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={"method": method, "params": {"name": "jira__search_issues"}},
            )
            assert response.status_code == 200
            assert response.json()["error"]["code"] == -32000
            assert "No spec available" in response.json()["error"]["message"]
    assert not mcp.call_tool_invocations
    assert not manager.spawns


@pytest.mark.asyncio
async def test_background_title_for_missing_child_returns_typed_error() -> None:
    """A stale child's title request fails with the typed code, not an unhandled error."""
    conv = "conv_missing_child_title"
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=False)

    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=_ContractSnapshotClient(conv),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await http.post(
            f"/v1/sessions/{conv}/background-title",
            json={"prompt": "summarize this", "agent_id": "ag_contract_root"},
        )
    assert response.status_code == 410, response.text
    assert response.json()["error"] == "sub_agent_unresolved"
    assert "worker" in response.json()["detail"]
    assert not manager.spawns


@pytest.mark.asyncio
async def test_background_title_for_native_mirror_uses_owning_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror's display name is not a bundle child; its summary resolves via the parent."""
    owner, mirror = "conv_title_owner", "conv_title_mirror"
    server = _ContractSnapshotClient(owner)
    original_get = server.get
    calls = []

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(200, json=_mirror_snapshot(owner))
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        calls.append(session_id)
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    monkeypatch.setattr(runner_app, "generator_spec_for_harness", lambda harness: None)
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        response = await http.post(
            f"/v1/sessions/{mirror}/background-title",
            json={"prompt": "summarize this", "agent_id": "ag_contract_root"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "unsupported", "title": None}
    assert calls == [owner]
    assert not manager.spawns


@pytest.mark.asyncio
async def test_native_mirror_with_cyclic_parent_fails_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt metadata naming a mirror as its own parent must not re-enter its lock."""
    mirror = "conv_cyclic_mirror"
    server = _ContractSnapshotClient(mirror)
    original_get = server.get

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(200, json=_mirror_snapshot(mirror))
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        response = await asyncio.wait_for(http.get(f"/v1/sessions/{mirror}/resources"), timeout=10)
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "internal_error"
    assert not manager.spawns


@pytest.mark.asyncio
async def test_native_mirrors_naming_each_other_fail_instead_of_deadlocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two mirrors recorded as each other's parent fail concurrently rather than hang."""
    first, second = "conv_mirror_a", "conv_mirror_b"
    server = _ContractSnapshotClient(first)
    original_get = server.get

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{first}"):
            return httpx.Response(200, json=_mirror_snapshot(second))
        if url.endswith(f"/v1/sessions/{second}"):
            return httpx.Response(200, json=_mirror_snapshot(first))
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        responses = await asyncio.wait_for(
            asyncio.gather(
                http.get(f"/v1/sessions/{first}/resources"),
                http.get(f"/v1/sessions/{second}/resources"),
            ),
            timeout=10,
        )
    assert [response.status_code for response in responses] == [500, 500]
    assert all(response.json()["error"]["code"] == "internal_error" for response in responses)
    assert not manager.spawns


@pytest.mark.asyncio
async def test_legacy_child_with_mirror_label_is_treated_as_a_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted row carrying a mirror label resolves through its parent.

    Clients can no longer write these labels, so a row that has them was
    stamped by the server; the runner keeps trusting them.
    """
    owner, child = "conv_legacy_owner", "conv_legacy_child"
    server = _ContractSnapshotClient(owner)
    original_get = server.get
    calls = []

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{child}"):
            return httpx.Response(
                200, json={**_mirror_snapshot(owner), "sub_agent_name": "worker_renamed"}
            )
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        calls.append(session_id)
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        response = await http.get(f"/v1/sessions/{child}/resources")
    assert response.status_code == 200, response.text
    assert calls == [owner]
    assert not manager.spawns


@pytest.mark.asyncio
async def test_background_title_metadata_outage_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata outage yields a retryable 503, never a permanent 410 for a mirror."""
    mirror = "conv_title_outage"
    server = _ContractSnapshotClient("conv_title_outage_owner")
    original_get = server.get

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(503)
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        response = await http.post(
            f"/v1/sessions/{mirror}/background-title",
            json={
                "prompt": "summarize this",
                "agent_id": "ag_contract_root",
                "sub_agent_name": "Explore",
            },
        )
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "spec_resolver_failed"
    assert not manager.spawns


@pytest.mark.asyncio
@pytest.mark.parametrize("display_name", _MIRROR_DISPLAY_NAMES)
@pytest.mark.parametrize("path", ["background", "known_harness", "no_harness"])
async def test_native_mirror_display_name_turn_explains_the_mirror(
    path: str, display_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn sent to a native mirror is refused with an explanation naming the mirror."""
    owner, mirror = "conv_mirror_turn_owner", "conv_mirror_turn"
    server = _ContractSnapshotClient(owner)
    original_get = server.get

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(200, json=_mirror_snapshot(owner, display_name))
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    manager = _RecordingManager(recording)
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        result = await _CONTRACT_ADAPTERS[path](http, mirror, recording)
        if path == "background":
            assert result["status"] == 202
            failure = _statuses(app, mirror)[-1]
            assert failure["status"] == "failed"
            error = failure["error"]
        else:
            assert result["status"] == 400
            error = result["error"]
    assert error["code"] == "invalid_input"
    assert f"mirrors the native sub-agent '{display_name}'" in error["message"]
    assert "renamed" not in error["message"]
    assert not manager.spawns
    assert not recording.posted_bodies


@pytest.mark.asyncio
@pytest.mark.parametrize("display_name", _MIRROR_DISPLAY_NAMES)
async def test_native_mirror_display_name_init_explains_the_mirror(display_name: str) -> None:
    """Session init for a native mirror is refused with an explanation naming the mirror."""
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
        RunnerSessionInitSnapshot,
    )

    owner, mirror = "conv_mirror_init_owner", "conv_mirror_init"
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id=mirror,
        agent_id="ag_contract_root",
        sub_agent_name=display_name,
        snapshot=RunnerSessionInitSnapshot(
            created_at=0,
            updated_at=0,
            labels={"omnigent.wrapper": "claude-code-native-ui-subagent"},
            parent_session_id=owner,
        ),
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    manager = _RecordingManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=_ContractSnapshotClient(owner),
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        created = await http.post(
            "/v1/sessions",
            json={
                "session_id": mirror,
                "agent_id": "ag_contract_root",
                "sub_agent_name": display_name,
                "session_init": envelope.model_dump(mode="json"),
            },
        )
    assert created.status_code == 400, created.text
    assert created.json()["error"]["code"] == "invalid_input"
    assert f"mirrors the native sub-agent '{display_name}'" in created.json()["error"]["message"]
    assert not manager.spawns


@pytest.mark.asyncio
@pytest.mark.parametrize("display_name", _MIRROR_DISPLAY_NAMES)
async def test_native_mirror_background_turn_without_agent_id_explains_the_mirror(
    display_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background turn whose body names no agent still cannot run a mirror on its owner."""
    owner, mirror = "conv_mirror_bare_owner", "conv_mirror_bare"
    server = _ContractSnapshotClient(owner)
    original_get = server.get

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(200, json=_mirror_snapshot(owner, display_name))
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    manager = _RecordingManager(recording)
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        response = await http.post(
            f"/v1/sessions/{mirror}/events",
            json={"type": "message", "role": "user", "content": "hi"},
        )
        assert response.status_code == 202, response.text
        await _await_bg_turn_task(mirror)
        failure = _statuses(app, mirror)[-1]
    assert failure["status"] == "failed"
    assert failure["error"]["code"] == "invalid_input"
    assert f"mirrors the native sub-agent '{display_name}'" in failure["error"]["message"]
    assert not manager.spawns
    assert not recording.posted_bodies


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["known_harness", "no_harness"])
async def test_direct_stream_to_mirror_is_refused_even_during_owner_outage(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirror is refused before its owner is resolved, and the turn slot is released."""
    owner, mirror = "conv_mirror_outage_owner", "conv_mirror_outage"
    server = _ContractSnapshotClient(owner)
    original_get = server.get
    owner_down = True

    async def get(url: str, **kwargs: Any) -> Any:
        if url.endswith(f"/v1/sessions/{mirror}"):
            return httpx.Response(200, json=_mirror_snapshot(owner))
        if url.endswith(f"/v1/sessions/{owner}") and owner_down:
            return httpx.Response(503)
        return await original_get(url, **kwargs)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return _contract_root_spec(with_child=True)

    monkeypatch.setattr(server, "get", get)
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    manager = _RecordingManager(recording)
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=server,
    )  # type: ignore[arg-type]
    async with _runner_test_client(app) as http:
        for _ in range(2):
            # The slot is released each time: the follow-up is answered, not buffered.
            result = await _CONTRACT_ADAPTERS[path](http, mirror, recording)
            assert result["status"] == 400, result
            assert result["error"]["code"] == "invalid_input"
            assert "mirrors the native sub-agent 'Explore'" in result["error"]["message"]
            owner_down = False
    assert not manager.spawns
    assert not recording.posted_bodies
