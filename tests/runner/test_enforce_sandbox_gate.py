"""
Integration tests for the ``sys_agent_start`` policy gate in the runner.

Verifies the full flow: runner resolves spec → policy gate fires →
sandbox override applied → spawn env reflects the forced config.

Uses the same ``_FakeProcessManager`` + ``create_runner_app`` pattern
as ``test_app_sessions_native.py``.  The process manager captures the
``env`` dict passed to ``get_client``, and we assert on the
``HARNESS_CLAUDE_SDK_OS_ENV`` value to confirm the sandbox was forced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
)
from tests.runner.conftest import _drain_session_event_queue
from tests.runner.conftest import _FakeProcessManager as _TurnProcessManager
from tests.runner.conftest import _ScriptedHarnessClient as _TurnHarnessClient
from tests.runner.helpers import NullServerClient

# ── Stubs ────────────────────────────────────────────────────────────────


class _ScriptedHarnessClient:
    """Minimal harness client stub — never called in these tests.

    Session creation only spawns the harness; it doesn't run a
    turn, so the client is never invoked.
    """

    async def close(self) -> None:
        """No-op close."""


class _FakeProcessManager:
    """Captures ``get_client`` calls so tests can inspect spawn env.

    :param client: The harness client stub returned by
        :meth:`get_client`.
    """

    handles_tool_dispatch = True

    def __init__(self, client: _ScriptedHarnessClient) -> None:
        """Wrap *client* so :meth:`get_client` returns it.

        :param client: Stub returned for every ``get_client`` call.
        """
        self._client = client
        self._sessions: set[str] = set()
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ScriptedHarnessClient:
        """Return the stub and record the call for assertions.

        :param conversation_id: Session id, e.g. ``"conv_test"``.
        :param harness: Harness name, e.g. ``"claude-sdk"``.
        :param env: Spawn-env dict built by the runner.
        :returns: The fixed stub client.
        """
        self.get_client_calls.append((conversation_id, harness, env))
        self._sessions.add(conversation_id)
        return self._client

    def has_session(self, conversation_id: str) -> bool:
        """Check if a session was registered.

        :param conversation_id: Session id.
        :returns: ``True`` if ``get_client`` was called for it.
        """
        return conversation_id in self._sessions

    async def forward_cancel(self, conversation_id: str) -> bool:
        """No-op cancel stub.

        :param conversation_id: Session id.
        :returns: Always ``True``.
        """
        return True

    async def release(self, conversation_id: str) -> None:
        """No-op release stub.

        :param conversation_id: Session id.
        """
        self._sessions.discard(conversation_id)

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub (issue #1414)."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub (issue #1414)."""
        del conversation_id


@contextlib.asynccontextmanager
async def _runner_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client for the runner app.

    :param app: The runner FastAPI app.
    :yields: An ``httpx.AsyncClient`` pointed at the ASGI transport.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


def _spec_with_enforce_sandbox(
    *,
    sandbox_type: str = "linux_bwrap",
    allow_network: bool = False,
    write_paths: list[str] | None = None,
) -> AgentSpec:
    """Build an ``AgentSpec`` with ``enforce_sandbox`` attached.

    The spec declares ``os_env`` with ``sandbox.type: none`` so the
    policy has something to override.

    :param sandbox_type: Sandbox type the policy forces.
    :param allow_network: Network flag the policy forces.
    :param write_paths: Write paths the policy forces. ``None``
        means the policy inherits the agent's existing paths.
    :returns: An ``AgentSpec`` with guardrails containing the
        ``enforce_sandbox`` policy.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    factory_args: dict[str, Any] = {
        "sandbox_type": sandbox_type,
        "allow_network": allow_network,
    }
    if write_paths is not None:
        factory_args["write_paths"] = write_paths

    return AgentSpec(
        spec_version=1,
        name="sandbox-test-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        os_env=OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="force_bwrap",
                    on=None,
                    function=FunctionRef(
                        path="omnigent.policies.builtins.safety.enforce_sandbox",
                        arguments=factory_args,
                    ),
                ),
            ],
        ),
    )


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enforce_sandbox_overrides_spawn_env() -> None:
    """The ``enforce_sandbox`` policy forces bwrap in the spawn env.

    Creates a session with ``sandbox.type: none`` in the spec, but
    the ``enforce_sandbox`` policy is attached. After session
    creation, the spawn env's ``HARNESS_CLAUDE_SDK_OS_ENV`` should
    contain ``"type": "linux_bwrap"`` instead of ``"none"``.

    If the sandbox type is still ``"none"``, the policy gate did
    not fire or the override was not applied before
    ``_build_spawn_env_from_spec``.
    """
    spec = _spec_with_enforce_sandbox(
        sandbox_type="linux_bwrap",
        allow_network=False,
        write_paths=["."],
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Always return the test spec.

        :param agent_id: Ignored.
        :param session_id: Ignored.
        :returns: The pre-built spec with enforce_sandbox.
        """
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_sandbox", "agent_id": "ag_test"},
        )

    # Session created successfully.
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"

    # Process manager was called — harness subprocess would have spawned.
    assert pm.get_client_calls, "get_client was never called — harness was not spawned"
    _conv_id, _harness, env = pm.get_client_calls[-1]
    assert env is not None, "spawn_env was None — _build_spawn_env_from_spec returned nothing"

    # The spawn env must carry the forced sandbox config.
    os_env_json = env.get("HARNESS_CLAUDE_SDK_OS_ENV")
    assert os_env_json is not None, (
        "HARNESS_CLAUDE_SDK_OS_ENV missing from spawn env — "
        "os_env was not serialized into the harness env"
    )
    os_env = json.loads(os_env_json)
    sandbox = os_env.get("sandbox", {})

    # Policy forced linux_bwrap — spec declared "none".
    # If type is still "none", the policy gate didn't fire.
    assert sandbox["type"] == "linux_bwrap", (
        f"Expected sandbox type 'linux_bwrap' (forced by policy), "
        f"got '{sandbox['type']}'. The sys_agent_start gate did not "
        f"apply the enforce_sandbox override before spawn."
    )
    # Policy forced allow_network=False.
    assert sandbox["allow_network"] is False, (
        f"Expected allow_network=False (forced by policy), got {sandbox['allow_network']!r}."
    )
    # Policy forced write_paths=["."].
    assert sandbox["write_paths"] == ["."], (
        f"Expected write_paths=['.'] (forced by policy), got {sandbox['write_paths']!r}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream", "caller_spawn_env"),
    [(True, False), (True, True), (False, False)],
    ids=["stream", "stream-with-caller-env", "background"],
)
@pytest.mark.parametrize("create_session", [True, False], ids=["created", "first-turn"])
async def test_enforce_sandbox_survives_agent_revision(
    *, stream: bool, caller_spawn_env: bool, create_session: bool
) -> None:
    """Freshly resolved specs must pass the start gate before every spawn."""
    resolved_specs: list[AgentSpec] = []
    enforced_write_paths = ["."]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        spec = _spec_with_enforce_sandbox(write_paths=enforced_write_paths)
        resolved_specs.append(spec)
        return spec

    harness_client = _TurnHarnessClient(
        [
            'data: {"type":"response.created","response":{"id":"resp_sandbox"}}\n\n',
            'data: {"type":"response.completed","response":{"id":"resp_sandbox"}}\n\n',
        ]
    )
    manager = _TurnProcessManager(harness_client)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    session_id = f"conv_sandbox_revision_{stream}_{create_session}"
    expected_sandbox = {
        "type": "linux_bwrap",
        "allow_network": False,
        "write_paths": ["."],
    }

    async with _runner_client(app) as client:
        if create_session:
            response = await client.post(
                "/v1/sessions", json={"session_id": session_id, "agent_id": "ag_test"}
            )
            assert response.status_code == 201, response.text
            assert len(manager.get_client_calls) == 1
            initial_env = manager.get_client_calls[0][2]
            assert initial_env is not None
            initial_sandbox = json.loads(initial_env["HARNESS_CLAUDE_SDK_OS_ENV"])["sandbox"]
            assert {key: initial_sandbox[key] for key in expected_sandbox} == expected_sandbox

        for turn_index, revision in enumerate(("initial", "updated"), start=1):
            if revision == "updated":
                enforced_write_paths = []
                expected_sandbox["write_paths"] = []
            resolve_count = len(resolved_specs)
            spawn_count = len(manager.get_client_calls)
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                params={"stream": "true"} if stream else {},
                json={
                    "type": "message",
                    "role": "user",
                    **({} if create_session and caller_spawn_env else {"agent_id": "ag_test"}),
                    "agent_revision": revision,
                    "content": [{"type": "input_text", "text": "Check sandbox policy."}],
                    **(
                        {
                            "harness": "claude-sdk",
                            "spawn_env": {
                                "HARNESS_CLAUDE_SDK_OS_ENV": '{"sandbox":{"type":"none"}}'
                            },
                        }
                        if caller_spawn_env
                        else {}
                    ),
                },
            )
            assert response.status_code == (200 if stream else 202), response.text
            if not stream:
                turn_task = app.state.active_turns.get(session_id)
                if turn_task is not None:
                    await asyncio.wait_for(turn_task, timeout=5)

            assert len(resolved_specs) > resolve_count, "revision must resolve a fresh spec"
            assert len({id(spec) for spec in resolved_specs}) == len(resolved_specs)
            assert len(harness_client.posted_bodies) == turn_index
            assert len(manager.get_client_calls) > spawn_count
            for conversation_id, harness, env in manager.get_client_calls[spawn_count:]:
                assert conversation_id == session_id
                assert harness == "claude-sdk"
                assert env is not None
                sandbox = json.loads(env["HARNESS_CLAUDE_SDK_OS_ENV"])["sandbox"]
                assert {key: sandbox[key] for key in expected_sandbox} == expected_sandbox, (
                    f"revision {revision!r} discarded the enforced sandbox"
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream", "caller_spawn_env"),
    [(True, False), (True, True), (False, False)],
    ids=["stream", "stream-with-caller-env", "background"],
)
async def test_agent_revision_with_unevaluable_start_policy_blocks_dispatch(
    stream: bool, caller_spawn_env: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A policy broken by a revision must stop dispatch to the existing worker."""
    resolve_count = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolve_count
        del agent_id, session_id
        resolve_count += 1
        spec = _spec_with_enforce_sandbox()
        if resolve_count > 1:
            assert spec.guardrails is not None
            spec.guardrails.policies.append(
                FunctionPolicySpec(
                    name="broken_start_policy",
                    on=None,
                    function=FunctionRef(path="omnigent.policies.builtins.does_not_exist"),
                )
            )
        return spec

    harness_client = _TurnHarnessClient([])
    manager = _TurnProcessManager(harness_client)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    session_id = f"conv_broken_start_policy_{stream}"

    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": "ag_test"}
        )
        assert response.status_code == 201, response.text
        assert len(manager.get_client_calls) == 1
        _drain_session_event_queue(app.state.session_event_queues.get(session_id))

        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            params={"stream": "true"} if stream else {},
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_test",
                "agent_revision": "broken",
                "content": [{"type": "input_text", "text": "Check sandbox policy."}],
                **(
                    {
                        "harness": "claude-sdk",
                        "spawn_env": {"HARNESS_CLAUDE_SDK_OS_ENV": '{"sandbox":{"type":"none"}}'},
                    }
                    if caller_spawn_env
                    else {}
                ),
            },
        )
        assert response.status_code == (503 if stream else 202), response.text
        if stream:
            assert response.json()["error"] == "spec_resolver_failed"
            assert "AgentStartPolicyError" in response.json()["detail"]
        else:
            turn_task = app.state.active_turns.get(session_id)
            if turn_task is not None:
                await asyncio.wait_for(turn_task, timeout=5)

        events = _drain_session_event_queue(app.state.session_event_queues.get(session_id))
        failures = [
            event
            for event in events
            if event.get("type") == "session.status" and event.get("status") == "failed"
        ]
        assert len(failures) == 1
        assert ("AgentStartPolicyError" if stream else "broken_start_policy") in failures[0][
            "error"
        ]["message"]
        assert "broken_start_policy" in caplog.text
        assert resolve_count > 1
        assert len(manager.get_client_calls) == 1, "broken policy must not spawn a replacement"
        assert not harness_client.posted_bodies, "broken policy must not reach the existing worker"
        assert session_id not in app.state.active_turns


@pytest.mark.asyncio
async def test_enforce_sandbox_no_policy_leaves_spec_unchanged() -> None:
    """Without ``enforce_sandbox``, the spawn env uses the spec's sandbox as-is.

    Control test: ensures the gate is a no-op when no policy applies.
    If this fails, the gate is mutating specs unconditionally.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    spec = AgentSpec(
        spec_version=1,
        name="no-policy-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        os_env=OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        # No guardrails — no policies.
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Always return the policy-free spec.

        :param agent_id: Ignored.
        :param session_id: Ignored.
        :returns: Spec with no guardrails.
        """
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_nopolicy", "agent_id": "ag_test"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    _conv_id, _harness, env = pm.get_client_calls[-1]
    assert env is not None

    os_env_json = env.get("HARNESS_CLAUDE_SDK_OS_ENV")
    assert os_env_json is not None
    os_env = json.loads(os_env_json)
    sandbox = os_env.get("sandbox", {})

    # No policy attached — sandbox stays "none" as declared in spec.
    assert sandbox["type"] == "none", (
        f"Expected sandbox type 'none' (no policy), got '{sandbox['type']}'. "
        f"The gate is mutating specs even when no policy applies."
    )


class _ModelOverrideServerClient(NullServerClient):
    """Server client whose session GET returns a persisted model_override.

    Mirrors the real server projecting the ``/model`` override in the
    session snapshot, so ``create_session`` can seed the initial spawn.
    """

    def __init__(self, *, session_id: str, agent_id: str, model_override: str) -> None:
        self._session_id = session_id
        self._agent_id = agent_id
        self._model_override = model_override

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Return the snapshot for the session GET, empty 200 otherwise."""
        del kwargs
        if url == f"/v1/sessions/{self._session_id}":
            body = {
                "agent_id": self._agent_id,
                "model_override": self._model_override,
            }

            class _SnapResponse:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return body

                def raise_for_status(self) -> None:
                    """No-op: stub always succeeds."""

            return _SnapResponse()
        return self._Response()


@pytest.mark.asyncio
async def test_create_session_seeds_model_override_into_spawn_env() -> None:
    """Legacy-server compatibility: the override seeds the spawn env via GET.

    When the server sends only the legacy id-only init body (``session_id`` /
    ``agent_id`` / ``sub_agent_name``, no ``session_init`` envelope — an older
    server), ``_initialize_session`` has no envelope to read the persisted
    ``/model`` override from, so it falls back to a one-shot uncached GET and
    seeds the override into the initial spawn env. Current servers send the
    envelope instead (covered by
    :func:`test_create_session_seeds_model_override_from_envelope`). That the
    first turn then reuses the seeded subprocess without a respawn is proven
    against a real ``HarnessProcessManager`` by
    ``tests/runtime/harnesses/test_process_manager.py::
    test_get_client_seeds_model_and_reuses_without_respawn``.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    spec = AgentSpec(
        spec_version=1,
        name="model-seed-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        os_env=OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_ModelOverrideServerClient(  # type: ignore[arg-type]
            session_id="conv_model_seed",
            agent_id="ag_test",
            model_override="model-x",
        ),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_model_seed", "agent_id": "ag_test"},
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    # Exactly one spawn at create time — the seed means the first turn
    # will not need to respawn for the override model.
    assert len(pm.get_client_calls) == 1, (
        f"expected a single create-time spawn, got {pm.get_client_calls}"
    )
    _conv_id, _harness, env = pm.get_client_calls[-1]
    assert env is not None
    # The override is baked into the spawn env, so the child starts on it.
    assert env.get("HARNESS_CLAUDE_SDK_MODEL") == "model-x", (
        "create_session must seed the persisted model_override into the "
        f"initial spawn env; got {env.get('HARNESS_CLAUDE_SDK_MODEL')!r}"
    )


def _session_init_body(
    *, session_id: str, agent_id: str, model_override: str | None
) -> dict[str, Any]:
    """Build a protocol-v2 ``session_init`` POST body carrying model_override.

    Mirrors :func:`build_runner_session_init_payload` so the runner's init
    takes the modern envelope path (no snapshot GET) and reads the override
    straight from the envelope snapshot.
    """
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PAYLOAD_KEY,
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
        RunnerSessionInitSnapshot,
    )

    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="0.0.0.dev0",
        session_id=session_id,
        agent_id=agent_id,
        sub_agent_name=None,
        snapshot=RunnerSessionInitSnapshot(
            created_at=0,
            updated_at=0,
            model_override=model_override,
        ),
    )
    return {
        "session_id": session_id,
        "agent_id": agent_id,
        SESSION_INIT_PAYLOAD_KEY: envelope.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_create_session_seeds_model_override_from_envelope() -> None:
    """Envelope path: the override seeds the spawn env with no snapshot GET.

    The modern server ships the persisted ``/model`` override inside the
    ``session_init`` envelope. ``_initialize_session`` must read it straight
    from ``init_context.envelope.snapshot.model_override`` and seed the
    initial spawn — never falling back to a server GET. The server client
    here would raise on any GET, proving the value came from the envelope.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    class _NoSnapshotGetServerClient(NullServerClient):
        """Fails a session-snapshot GET so a stray fallback read is caught.

        Other GETs (e.g. history ``/items``) pass through to the benign
        base stub — only the snapshot URL is forbidden, since the envelope
        path must read model_override from the envelope, not a GET.
        """

        async def get(self, url: str, **kwargs: Any) -> Any:
            if url == "/v1/sessions/conv_env_seed":
                raise AssertionError(
                    "envelope path must not GET the session snapshot for "
                    "model_override; it must read the envelope"
                )
            return await super().get(url, **kwargs)

    spec = AgentSpec(
        spec_version=1,
        name="model-seed-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        os_env=OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NoSnapshotGetServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json=_session_init_body(
                session_id="conv_env_seed",
                agent_id="ag_test",
                model_override="model-x",
            ),
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    assert len(pm.get_client_calls) == 1, (
        f"expected a single create-time spawn, got {pm.get_client_calls}"
    )
    _conv_id, _harness, env = pm.get_client_calls[-1]
    assert env is not None
    assert env.get("HARNESS_CLAUDE_SDK_MODEL") == "model-x", (
        "_initialize_session must seed the envelope's model_override into the "
        f"initial spawn env; got {env.get('HARNESS_CLAUDE_SDK_MODEL')!r}"
    )


@pytest.mark.asyncio
async def test_reinit_after_model_switch_seeds_fresh_override() -> None:
    """A re-init after a /model switch seeds the NEW model, never a stale one.

    ``model_override`` is mutable, so it must not be cached in the long-lived
    identity snapshot: a stale value would reseed the old model on a later
    re-init and force the very model-switch respawn the seeding removes. The
    legacy fallback reads it fresh each init, so switching the server-side
    override between two inits must change the seeded spawn env.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    spec = AgentSpec(
        spec_version=1,
        name="model-seed-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        os_env=OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient())
    server = _ModelOverrideServerClient(
        session_id="conv_switch",
        agent_id="ag_test",
        model_override="model-a",
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server,  # type: ignore[arg-type]
    )

    body = {"session_id": "conv_switch", "agent_id": "ag_test"}
    async with _runner_client(app) as client:
        resp_a = await client.post("/v1/sessions", json=body)
        # User switches /model server-side; the runner must NOT serve a cached
        # "model-a" on the next init.
        server._model_override = "model-b"
        resp_b = await client.post("/v1/sessions", json=body)

    assert resp_a.status_code == 201, resp_a.text
    assert resp_b.status_code == 201, resp_b.text
    assert len(pm.get_client_calls) == 2, f"expected two spawns, got {pm.get_client_calls}"
    assert pm.get_client_calls[0][2].get("HARNESS_CLAUDE_SDK_MODEL") == "model-a"
    assert pm.get_client_calls[1][2].get("HARNESS_CLAUDE_SDK_MODEL") == "model-b", (
        "re-init after a /model switch must seed the fresh override, not a "
        f"stale cached one; got {pm.get_client_calls[1][2].get('HARNESS_CLAUDE_SDK_MODEL')!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "background"])
async def test_cached_start_policy_tracks_harness_override_changes(
    stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target changes reapply launch policy to the declared sandbox without accumulation."""
    from omnigent.policies import FunctionPolicy

    start_harnesses: list[str] = []

    def _target_sandbox(event: dict[str, Any]) -> dict[str, Any]:
        data = event["data"]
        arguments = data["arguments"]
        harness = arguments["harness"]
        start_harnesses.append(harness)
        sandbox = arguments["sandbox"]
        sandbox["read_paths"].append(f"/tmp/{harness}-read")
        return {
            "result": "ALLOW",
            "data": {
                **data,
                "arguments": {
                    **arguments,
                    "sandbox": {
                        **sandbox,
                        "type": "linux_bwrap" if harness == "kimi" else "none",
                        "allow_network": harness != "kimi",
                    },
                },
            },
        }

    def _resolve_policy(policy_spec: FunctionPolicySpec) -> FunctionPolicy:
        return FunctionPolicy(policy_spec, _target_sandbox)

    monkeypatch.setattr("omnigent.runner.policy.resolve_function_policy", _resolve_policy)
    declared_spec = _spec_with_enforce_sandbox()
    assert declared_spec.os_env is not None
    assert declared_spec.os_env.sandbox is not None
    declared_spec.os_env.sandbox.read_paths = ["."]
    resolve_count = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolve_count
        assert agent_id == "ag_test"
        resolve_count += 1
        return declared_spec

    harness_client = _TurnHarnessClient(
        [
            'data: {"type":"response.created","response":{"id":"resp_target"}}\n\n',
            'data: {"type":"response.completed","response":{"id":"resp_target",'
            '"status":"completed"}}\n\n',
        ]
    )
    manager = _TurnProcessManager(harness_client)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    session_id = f"conv_start_policy_target_{stream}"

    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": "ag_test"}
        )
        assert response.status_code == 201, response.text
        initial_env = manager.get_client_calls[0][2]
        assert initial_env is not None
        previous_policy = json.loads(initial_env["HARNESS_CLAUDE_SDK_OS_ENV"])
        previous_harness = "claude-sdk"

        targets = (None, None, "kimi", "kimi", "claude-sdk")
        for turn_index, harness_override in enumerate(targets, start=1):
            harness = harness_override or "claude-sdk"
            probe_count = len(start_harnesses)
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                params={"stream": "true"} if stream else {},
                json={
                    "type": "message",
                    "role": "user",
                    **({"harness_override": harness_override} if harness_override else {}),
                    "content": [{"type": "input_text", "text": "Check target sandbox policy."}],
                },
            )
            assert response.status_code == (200 if stream else 202), response.text
            if not stream:
                turn_task = app.state.active_turns.get(session_id)
                if turn_task is not None:
                    await asyncio.wait_for(turn_task, timeout=5)
            if turn_index in (1, 2) or (not stream and turn_index == 4):
                assert len(start_harnesses) == probe_count

            assert session_id not in app.state.active_turns
            assert len(harness_client.posted_bodies) == turn_index
            conversation_id, actual_harness, env = manager.get_client_calls[-1]
            assert conversation_id == session_id
            assert actual_harness == harness
            assert env is not None
            env_key = f"HARNESS_{harness.upper().replace('-', '_')}_OS_ENV"
            policy = json.loads(env[env_key])
            sandbox = policy["sandbox"]
            assert sandbox["type"] == ("linux_bwrap" if harness == "kimi" else "none")
            assert sandbox["allow_network"] is (harness != "kimi")
            assert sandbox["read_paths"] == [".", f"/tmp/{harness}-read"]
            if harness == previous_harness:
                assert policy == previous_policy
            previous_harness, previous_policy = harness, policy

    assert "kimi" in start_harnesses
    if not stream:
        assert resolve_count == 3
    assert declared_spec.os_env.sandbox.type == "none"
    assert declared_spec.os_env.sandbox.read_paths == ["."]
