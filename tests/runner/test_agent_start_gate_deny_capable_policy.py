"""Verify deny-capable tool policies do not block session initialization."""

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
        """Record the spawn and return the stub client."""
        self.get_client_calls.append((conversation_id, harness, env))
        self._sessions.add(conversation_id)
        return self._client

    def has_session(self, conversation_id: str) -> bool:
        """Return whether the session spawned."""
        return conversation_id in self._sessions

    async def forward_cancel(self, conversation_id: str) -> bool:
        """Accept cancellation."""
        del conversation_id
        return True

    async def release(self, conversation_id: str) -> None:
        """Forget a released session."""
        self._sessions.discard(conversation_id)

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub."""
        del conversation_id


@contextlib.asynccontextmanager
async def _runner_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an ASGI client for the runner app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


def _spec_with_deny_capable_policy() -> AgentSpec:
    """Build a spec whose policy denies unlisted tool calls."""
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
    """A fail-closed tool policy still permits session initialization."""
    spec = _spec_with_deny_capable_policy()
    pm = _FakeProcessManager()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
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
        assert pm.has_session(session_id), "harness was not spawned"
        assert session_id in runner_app_module._session_inboxes_ref, (
            "session inbox was not created — sub-agent dispatch would fail "
            "with 'requires parent session inbox'"
        )
    finally:
        runner_app_module._session_inboxes_ref.pop(session_id, None)
