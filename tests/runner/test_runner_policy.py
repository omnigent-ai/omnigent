"""Tests for ``_evaluate_policy_via_omnigent`` fail-open / fail-closed.

The runner proxies harness policy-evaluation requests to the Omnigent
server and posts the verdict back to the harness. When that round-trip
errors or returns non-200 the default verdict must be *phase-aware*:

- LLM_REQUEST / LLM_RESPONSE fail OPEN (a transient outage must not hang
  the turn — those gates are advisory).
- TOOL_CALL fails CLOSED — for connector-native MCP tools the harness
  ``can_use_tool`` callback that consumes this verdict is the only
  enforcement point, so an unevaluable policy must block the call.
- TOOL_RESULT fails OPEN: the tool has already executed by then, so
  denying only blocks an already-incurred side effect.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runner.app import _evaluate_policy_via_omnigent


class _RaisingServerClient:
    """Server client whose ``/policies/evaluate`` POST always errors."""

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("AP unreachable")


class _StatusServerClient:
    """Server client returning a fixed status (and optional JSON body)."""

    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self._status = status
        self._body = body or {}

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(self._status, json=self._body)


class _CapturingHarnessClient:
    """Harness client that records the verdict body posted back."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.posted.append(json)
        return httpx.Response(200, json={})


async def _run(server_client: Any, phase: str) -> dict[str, Any]:
    """Drive the proxy once and return the verdict body posted to the harness.

    :param server_client: Stub Omnigent-server client.
    :param phase: Proto phase string, e.g. ``"PHASE_TOOL_CALL"``.
    :returns: The single ``policy_verdict`` body the harness received.
    """
    harness = _CapturingHarnessClient()
    await _evaluate_policy_via_omnigent(
        server_client=server_client,
        harness_client=harness,
        conversation_id="conv_test",
        evaluation_id="poleval_test",
        phase=phase,
        data={"name": "mcp__github__merge_pull_request", "arguments": {}},
    )
    assert len(harness.posted) == 1, "exactly one verdict must be delivered"
    return harness.posted[0]


async def test_tool_call_error_fails_closed() -> None:
    """A round-trip error on the TOOL_CALL phase yields a DENY verdict."""
    verdict = await _run(_RaisingServerClient(), "PHASE_TOOL_CALL")
    assert verdict["action"] == "POLICY_ACTION_DENY", verdict
    assert verdict.get("reason"), "fail-closed verdict should carry a reason"


async def test_tool_call_non_200_fails_closed() -> None:
    """A non-200 from the server on the TOOL_CALL phase yields a DENY verdict."""
    verdict = await _run(_StatusServerClient(500), "PHASE_TOOL_CALL")
    assert verdict["action"] == "POLICY_ACTION_DENY", verdict


@pytest.mark.parametrize("phase", ["PHASE_LLM_REQUEST", "PHASE_LLM_RESPONSE", "PHASE_TOOL_RESULT"])
async def test_non_tool_call_phase_error_fails_open(phase: str) -> None:
    """Fail-open is preserved off the TOOL_CALL phase: an error yields ALLOW.

    LLM phases are advisory; TOOL_RESULT fails open too because the tool
    has already executed by then, so denying would only block an
    already-incurred side effect (maintainer design decision — see PR
    review thread).
    """
    verdict = await _run(_RaisingServerClient(), phase)
    assert verdict["action"] == "POLICY_ACTION_ALLOW", verdict


async def test_success_verdict_is_passed_through_unchanged() -> None:
    """A 200 response is honored verbatim — the default never overrides it."""
    server = _StatusServerClient(200, {"result": "POLICY_ACTION_ALLOW", "reason": None})
    verdict = await _run(server, "PHASE_TOOL_CALL")
    assert verdict["action"] == "POLICY_ACTION_ALLOW", verdict


async def test_success_deny_verdict_passed_through() -> None:
    """A real DENY from the server is delivered as-is with its reason."""
    server = _StatusServerClient(200, {"result": "POLICY_ACTION_DENY", "reason": "blocked"})
    verdict = await _run(server, "PHASE_TOOL_CALL")
    assert verdict["action"] == "POLICY_ACTION_DENY", verdict
    assert verdict["reason"] == "blocked"
