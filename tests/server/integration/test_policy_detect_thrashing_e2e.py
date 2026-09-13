"""Integration test for the detect_thrashing context policy.

Exercises the detect_thrashing policy through the
``POST /v1/sessions/{id}/policies/evaluate`` endpoint, verifying
that error outcomes accumulate across evaluations in session_state
and that the policy transitions from ALLOW to ASK after the
consecutive-error threshold is reached.

Uses ``default_policies`` monkeypatch to inject the policy without
going through the registry allowlist — same pattern as
``test_policy_composition_e2e.py``.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PhaseSelector
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_DETECT_THRASHING = "omnigent.policies.builtins.context.detect_thrashing"


def _install_thrashing_policy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    consecutive_threshold: int = 3,
    window: int = 10,
    window_error_rate: float = 0.0,
    action: str = "ASK",
) -> None:
    """Inject detect_thrashing as a default policy.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param consecutive_threshold: Consecutive errors before firing.
    :param window: Rolling window size.
    :param window_error_rate: Error rate threshold (0 disables).
    :param action: ASK or DENY.
    """
    original_caps = get_caps()
    policy = FunctionPolicySpec(
        name="thrashing_guard",
        on=[PhaseSelector(phase=Phase.TOOL_RESULT)],
        function=FunctionRef(
            path=_DETECT_THRASHING,
            arguments={
                "consecutive_threshold": consecutive_threshold,
                "window": window,
                "window_error_rate": window_error_rate,
                "action": action,
            },
        ),
    )
    patched_caps = dataclasses.replace(
        original_caps,
        default_policies=[policy],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )


def _tool_result_request(result: str) -> dict[str, Any]:
    """Build a PHASE_TOOL_RESULT EvaluationRequest.

    :param result: The tool output string.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_RESULT",
            "target": "",
            "data": {"result": result},
            "context": {},
            "request_data": {"name": "Bash", "arguments": {"command": "ls"}},
        },
    }


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _evaluate(
    client: httpx.AsyncClient,
    session_id: str,
    result: str,
) -> dict[str, Any]:
    """Post a tool_result evaluation and return the response body.

    :param client: Test HTTP client.
    :param session_id: Target session.
    :param result: Tool output string.
    :returns: Parsed response JSON.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_result_request(result),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_thrashing_fires_after_consecutive_errors(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After N consecutive tool errors, detect_thrashing returns ASK.

    1. Install detect_thrashing with consecutive_threshold=3.
    2. Send 2 error tool_results — expect ALLOW (below threshold).
    3. Send a 3rd error — expect ASK (threshold reached).
    4. Send a success — expect ALLOW (consecutive run broken).
    """
    _install_thrashing_policy(monkeypatch, consecutive_threshold=3)
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Errors 1 and 2: below threshold → ALLOW
    for i in range(2):
        body = await _evaluate(client, session_id, "Error: file not found")
        assert body["result"] == "POLICY_ACTION_ALLOW", (
            f"Error {i + 1}/3 should ALLOW (below threshold), got {body['result']}"
        )

    # Error 3: reaches threshold → ASK
    body = await _evaluate(client, session_id, "Error: file not found")
    assert body["result"] == "POLICY_ACTION_ASK", (
        f"Error 3/3 should ASK (threshold reached), got {body['result']}"
    )

    # Success breaks the consecutive run → ALLOW
    body = await _evaluate(client, session_id, "file written successfully")
    assert body["result"] == "POLICY_ACTION_ALLOW", (
        f"Success after errors should ALLOW (run broken), got {body['result']}"
    )
