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

Verdict *delivery* failures must also be attributed by cause: a dead
harness channel is an upstream disconnect/teardown consequence logged at
WARNING with a structured reason, while a live harness rejecting the
verdict (non-2xx) or an unexpected error stays an ERROR naming the mode.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from omnigent.runner.app import _evaluate_policy_via_omnigent

_RUNNER_LOGGER = "omnigent.runner.app"


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


# ── ExecutorAdapter._stable_policy_evaluator fail-closed ──────────────────


@pytest.mark.parametrize(
    ("phase", "expected_action"),
    [
        ("PHASE_TOOL_CALL", "POLICY_ACTION_DENY"),
        ("PHASE_LLM_REQUEST", "POLICY_ACTION_ALLOW"),
        ("PHASE_LLM_RESPONSE", "POLICY_ACTION_ALLOW"),
        ("PHASE_TOOL_RESULT", "POLICY_ACTION_ALLOW"),
    ],
)
async def test_missing_context_tool_call_fails_closed(phase: str, expected_action: str) -> None:
    """No active turn context defaults TOOL_CALL to DENY, advisory phases to ALLOW."""
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: None)  # type: ignore[arg-type,return-value]
    # No turn is active: _current_ctx is None.
    assert adapter._current_ctx is None
    verdict = await adapter._stable_policy_evaluator(phase, {})
    assert verdict.action == expected_action, (phase, verdict)
    if expected_action == "POLICY_ACTION_DENY":
        assert verdict.reason, "fail-closed verdict should carry a reason"


# ── verdict-delivery failure attribution ────────────────────────────────────


class _DeadChannelHarnessClient:
    """Harness client whose channel is dead: every POST raises a transport error."""

    def __init__(self) -> None:
        self.attempts = 0

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.attempts += 1
        raise httpx.ConnectError("harness subprocess gone")


class _RejectingHarnessClient:
    """Live harness client that refuses the verdict with a fixed non-2xx status."""

    def __init__(self, status: int) -> None:
        self._status = status
        self.attempts = 0

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.attempts += 1
        return httpx.Response(self._status, json={})


class _ExplodingHarnessClient:
    """Harness client that fails delivery with a non-transport error."""

    def __init__(self) -> None:
        self.attempts = 0

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.attempts += 1
        raise ValueError("malformed event body")


async def _run_delivery(harness: Any) -> list[str]:
    """Drive one evaluation whose verdict delivery uses *harness*.

    :param harness: Stub harness client controlling how delivery fails.
    :returns: Conversation ids passed to ``on_delivery_failure``.
    """
    failures: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        failures.append(conv_id)

    await _evaluate_policy_via_omnigent(
        server_client=_StatusServerClient(200, {"result": "POLICY_ACTION_ALLOW"}),  # type: ignore[arg-type]
        harness_client=harness,
        conversation_id="conv_test",
        evaluation_id="poleval_test",
        phase="PHASE_LLM_REQUEST",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    return failures


async def test_dead_channel_delivery_is_attributed_warning_not_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead harness channel is an upstream/teardown consequence, not a defect.

    Delivery still retries once and still signals desync recovery, but the
    wrap-up record must be a WARNING carrying a structured reason — never an
    ERROR-level "delivery unacknowledged" record.
    """
    harness = _DeadChannelHarnessClient()
    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        failures = await _run_delivery(harness)

    assert harness.attempts == 2, "dead-channel delivery must retry exactly once"
    assert failures == ["conv_test"], "desync recovery must still be signaled"

    records = [r for r in caplog.records if r.name == _RUNNER_LOGGER]
    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]

    wrapups = [r for r in records if "undeliverable after retry" in r.getMessage()]
    assert len(wrapups) == 1, [r.getMessage() for r in records]
    assert wrapups[0].levelno == logging.WARNING
    assert "harness channel is dead" in wrapups[0].getMessage()
    assert (
        getattr(wrapups[0], "delivery_failure_reason", None) == "verdict_delivery_channel_dead"
    ), "wrap-up must carry a structured delivery-failure reason"


async def test_live_harness_rejection_stays_error_naming_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live harness refusing the verdict is a potential defect: ERROR, with the status."""
    harness = _RejectingHarnessClient(500)
    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        failures = await _run_delivery(harness)

    assert harness.attempts == 2, "non-2xx delivery must retry exactly once"
    assert failures == ["conv_test"]

    errors = [r for r in caplog.records if r.name == _RUNNER_LOGGER and r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "delivery unacknowledged (http_500)" in errors[0].getMessage()
    assert getattr(errors[0], "delivery_failure_reason", None) == "http_500"


async def test_unexpected_delivery_error_stays_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-transport delivery failure keeps its ERROR, attributed as unexpected."""
    harness = _ExplodingHarnessClient()
    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        failures = await _run_delivery(harness)

    assert harness.attempts == 1, "unexpected errors must not be retried"
    assert failures == ["conv_test"]

    errors = [r for r in caplog.records if r.name == _RUNNER_LOGGER and r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "delivery unacknowledged (unexpected)" in errors[0].getMessage()
    assert getattr(errors[0], "delivery_failure_reason", None) == "unexpected"
