"""
Session init must survive a fail-closed (deny-capable) tool_call policy.

The runner probes guardrails policies with a synthetic ``sys_agent_start``
tool call so start-aware policies (``enforce_sandbox``) can transform the
sandbox config. A generic allowlist policy whose terminal branch is DENY has
never heard of ``sys_agent_start`` and denies the probe — that verdict must
not abort session initialization: the server forwards messages even when the
init handshake fails, so a refusal here only strands the session
half-initialized (no parent inbox → ``sys_session_send`` breaks).

Uses the same ``_FakeProcessManager`` + ``create_runner_app`` pattern as
``test_enforce_sandbox_gate.py``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import app as runner_app_module
from omnigent.runner import create_runner_app
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PhaseSelector,
)
from tests.runner.helpers import NullServerClient

# The reporter-shaped fail-closed expression: allowlist a few tool names,
# DENY everything else. ``sys_agent_start`` is not on the allowlist.
_DENY_CAPABLE_EXPRESSION = (
    'event.type != "tool_call"\n'
    '  ? {"result": "ALLOW"}\n'
    "  : has(event.data.name)\n"
    "    && type(event.data.name) == string\n"
    '    && event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$")\n'
    '    ? {"result": "ALLOW"}\n'
    '    : {"result": "DENY"}\n'
)


class _ScriptedHarnessClient:
    """Minimal harness client stub — session init only spawns, never calls."""

    async def close(self) -> None:
        """No-op close."""


class _FakeProcessManager:
    """Captures ``get_client`` calls so tests can assert the harness spawned."""

    handles_tool_dispatch = True

    def __init__(self) -> None:
        self._client = _ScriptedHarnessClient()
        self._sessions: set[str] = set()
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ScriptedHarnessClient:
        """Record the spawn request and return the stub client.

        :param conversation_id: Session id, e.g. ``"conv_test"``.
        :param harness: Harness name, e.g. ``"claude-sdk"``.
        :param env: Spawn-env dict built by the runner.
        :returns: The fixed stub client.
        """
        self.get_client_calls.append((conversation_id, harness, env))
        self._sessions.add(conversation_id)
        return self._client

    def has_session(self, conversation_id: str) -> bool:
        """Return whether ``get_client`` was called for the session.

        :param conversation_id: Session id.
        :returns: ``True`` if the session spawned.
        """
        return conversation_id in self._sessions

    async def forward_cancel(self, conversation_id: str) -> bool:
        """No-op cancel stub.

        :param conversation_id: Session id.
        :returns: Always ``True``.
        """
        del conversation_id
        return True

    async def release(self, conversation_id: str) -> None:
        """No-op release stub.

        :param conversation_id: Session id.
        """
        self._sessions.discard(conversation_id)

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub."""
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


def _spec_with_deny_capable_policy() -> AgentSpec:
    """Build an ``AgentSpec`` guarded by the fail-closed CEL allowlist.

    :returns: An ``AgentSpec`` whose only policy allowlists a few tool
        names and DENYs every other tool call.
    """
    return AgentSpec(
        spec_version=1,
        name="deny-capable-policy-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="allowlist_then_deny",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.cel.cel_policy",
                        arguments={"expression": _DENY_CAPABLE_EXPRESSION},
                    ),
                ),
            ],
        ),
    )


@pytest.mark.asyncio
async def test_deny_capable_policy_does_not_block_session_init() -> None:
    """A fail-closed tool_call policy must not abort session initialization.

    Before the fix the synthetic ``sys_agent_start`` probe hit the policy's
    terminal DENY branch and ``POST /v1/sessions`` returned 403
    ``agent_start_denied`` — leaving the session without its inbox, so the
    first ``sys_session_send`` failed with ``requires parent session inbox``.
    """
    spec = _spec_with_deny_capable_policy()
    pm = _FakeProcessManager()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Always return the deny-capable-policy spec.

        :param agent_id: Ignored.
        :param session_id: Ignored.
        :returns: The pre-built spec.
        """
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    session_id = "conv_deny_capable_init"
    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": "ag_test"},
            )

        assert resp.status_code == 201, (
            f"Session init must succeed despite the deny-capable policy; "
            f"got {resp.status_code}: {resp.text}"
        )
        # The harness spawned and per-session state exists — most importantly
        # the inbox that sys_session_send needs for sub-agent dispatch.
        assert pm.has_session(session_id), "harness was not spawned"
        assert session_id in runner_app_module._session_inboxes_ref, (
            "session inbox was not created — sub-agent dispatch would fail "
            "with 'requires parent session inbox'"
        )
    finally:
        runner_app_module._session_inboxes_ref.pop(session_id, None)
