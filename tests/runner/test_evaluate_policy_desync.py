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
import pytest

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


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timed out"),
    ],
)
async def test_connect_failure_is_treated_as_dead_channel(exc: BaseException) -> None:
    """Review-2: a connect failure (subprocess already gone) retries-then-signals.

    If the harness exited before the verdict POST opens a socket (or the retry
    lands after it is gone) httpx raises ``ConnectError`` / ``ConnectTimeout``.
    Those must signal the desync — not fall into the generic log-and-swallow
    path, which would leave the harness ``evaluate_policy`` future parked 24h.
    """
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    harness = _DeadChannelHarnessClient(exc)
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_conn",
        evaluation_id="poleval_conn",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    assert harness.attempts == 2
    assert signaled == ["conv_conn"]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("pool timed out"),
    ],
)
async def test_delivery_timeout_is_treated_as_dead_channel(exc: BaseException) -> None:
    """A verdict-delivery timeout retries-then-signals, not parks for 24h.

    A wedged harness that accepts the socket but never acknowledges the POST
    raises a read/write/pool timeout. Those are unacknowledged verdicts just
    like a reset connection, so they must signal the desync — not fall into the
    generic log-and-swallow path that leaves the future parked.
    """
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    harness = _DeadChannelHarnessClient(exc)
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_timeout",
        evaluation_id="poleval_timeout",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    assert harness.attempts == 2
    assert signaled == ["conv_timeout"]


async def test_non_2xx_verdict_response_retries_then_signals() -> None:
    """A non-2xx verdict POST is an unacknowledged delivery: retry then signal.

    ``harness_client.post`` does not raise on 4xx/5xx, so a swallowed 500 reads
    as delivered while the harness never enqueued the verdict and its future
    stays parked. The status MUST be checked; a non-2xx retries once, then
    signals the desync.
    """
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    class _Non2xxHarness:
        def __init__(self) -> None:
            self.attempts = 0

        async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
            del json, timeout
            self.attempts += 1
            return httpx.Response(500, text="boom")

    harness = _Non2xxHarness()
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_500",
        evaluation_id="poleval_500",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    # Retried once (2 attempts), then signaled the desync.
    assert harness.attempts == 2
    assert signaled == ["conv_500"]


async def test_non_transport_delivery_error_signals_without_retry() -> None:
    """A non-transport delivery error still signals — the verdict is unacknowledged.

    An unexpected (non-channel) error is a code bug, not a transient fault, so it
    is NOT retried; but the harness future is still parked, so recovery must
    still be signalled. Retry policy stays selective; signalling is universal.
    """
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
    # One attempt only (no retry for a non-transport error) — but STILL signaled.
    assert harness.attempts == 1
    assert signaled == ["conv_q"]


async def test_3xx_verdict_response_is_unacknowledged_and_signals() -> None:
    """A 3xx verdict response is NOT a 2xx ack: retry then signal.

    Only 2xx means the harness enqueued the event. A 3xx (redirect) is not an
    acknowledgement — the parked future never resolves — so it must retry and
    then signal, exactly like a 5xx.
    """
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    class _RedirectHarness:
        def __init__(self) -> None:
            self.attempts = 0

        async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
            del json, timeout
            self.attempts += 1
            return httpx.Response(302, headers={"location": "/elsewhere"})

    harness = _RedirectHarness()
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_3xx",
        evaluation_id="poleval_3xx",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )
    assert harness.attempts == 2
    assert signaled == ["conv_3xx"]


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
