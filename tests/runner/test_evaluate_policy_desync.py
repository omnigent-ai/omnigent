"""Verdict-delivery desync tests for ``_evaluate_policy_via_omnigent`` (#1026).

When the policy verdict cannot be delivered back to the harness because its
channel is dead (a transport error that survives a retry), the parked harness
future can never be resolved — the executor would hang for
``_POLICY_EVAL_TIMEOUT_S`` (24h). The runner must instead retry ONCE on a
fresh connection and then signal the desync via ``on_delivery_failure`` so the
wedged turn is torn down. Non-transport delivery errors keep the legacy
best-effort log-and-swallow behavior (no retry, no signal).
"""

from __future__ import annotations

from typing import Any

import httpx

from omnigent.runner.app import _evaluate_policy_via_omnigent


class _OkServerClient:
    """Server client whose evaluate POST returns a real ALLOW verdict."""

    async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
        del json, timeout
        return httpx.Response(200, json={"result": "POLICY_ACTION_ALLOW", "reason": None})


class _DeadChannelHarnessClient:
    """Harness client whose verdict POST always raises a dead-channel error."""

    def __init__(self, exc: BaseException) -> None:
        self.attempts = 0
        self._exc = exc

    async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
        del json, timeout
        self.attempts += 1
        raise self._exc


async def test_verdict_delivery_failure_retries_then_signals() -> None:
    """A dead-channel verdict POST retries once, then fires on_delivery_failure."""
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    harness = _DeadChannelHarnessClient(httpx.RemoteProtocolError("peer closed connection"))
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_xyz",
        evaluation_id="poleval_1",
        phase="PHASE_TOOL_CALL",
        data={"name": "mcp__github__merge_pull_request", "arguments": {}},
        on_delivery_failure=_on_delivery_failure,
    )

    # Exactly two attempts (original + one fresh-connection retry).
    assert harness.attempts == 2
    # The desync was signaled with the conversation id.
    assert signaled == ["conv_xyz"]


async def test_httpcore_read_error_is_treated_as_dead_channel() -> None:
    """An httpcore-level read error also retries-then-signals."""
    import httpcore

    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    harness = _DeadChannelHarnessClient(httpcore.ReadError("read failed"))
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_abc",
        evaluation_id="poleval_2",
        phase="PHASE_LLM_REQUEST",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    assert harness.attempts == 2
    assert signaled == ["conv_abc"]


async def test_non_dead_channel_delivery_error_swallows_without_signal() -> None:
    """A non-transport delivery error keeps log-and-swallow: no retry, no signal."""
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    harness = _DeadChannelHarnessClient(ValueError("malformed body"))
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_q",
        evaluation_id="poleval_3",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    # One attempt only, and the desync was NOT signaled.
    assert harness.attempts == 1
    assert signaled == []


async def test_successful_delivery_does_not_signal() -> None:
    """A clean delivery posts exactly once and never signals a desync."""
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    class _OkHarness:
        def __init__(self) -> None:
            self.attempts = 0

        async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
            del json, timeout
            self.attempts += 1
            return httpx.Response(200, json={})

    harness = _OkHarness()
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_ok",
        evaluation_id="poleval_4",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    assert harness.attempts == 1
    assert signaled == []
