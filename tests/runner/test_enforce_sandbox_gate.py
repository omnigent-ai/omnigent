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

    def current_harness(self, conversation_id: str) -> str | None:
        """Return ``None`` — fake manager doesn't track harness names."""
        return None

    async def release(self, conversation_id: str) -> None:
        """No-op release stub.

        :param conversation_id: Session id.
        """
        self._sessions.discard(conversation_id)


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
