"""Tests for tool retry logic: timeout resolution, retry resolution, and execution."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnigent.runtime.tool_retry import (
    call_tool_with_timeout,
    execute_tool_with_retry,
    resolve_tool_retry,
    resolve_tool_timeout,
)
from omnigent.spec.types import RetryPolicy, ToolsConfig


@pytest.fixture()
def global_tools_config() -> ToolsConfig:
    """
    A ToolsConfig with explicit global defaults for timeout and retry.

    :returns: ToolsConfig with timeout=60, retry max_retries=2.
    """
    return ToolsConfig(
        timeout=60,
        retry=RetryPolicy(max_retries=2, backoff_base_s=1.0, backoff_max_s=10.0),
    )


@pytest.fixture()
def captured_events() -> list[dict[str, Any]]:
    """
    Mutable list that accumulates on_event dicts during test execution.

    :returns: Empty list that tests inspect after calling execute_tool_with_retry.
    """
    return []


@pytest.fixture()
def on_event(captured_events: list[dict[str, Any]]) -> MagicMock:
    """
    An on_event callback that records every dict it receives.

    :returns: A MagicMock whose side_effect appends to captured_events.
    """
    return MagicMock(side_effect=lambda evt: captured_events.append(evt))


# -- resolve_tool_timeout --


def test_resolve_tool_timeout_uses_per_tool_override(
    global_tools_config: ToolsConfig,
) -> None:
    """Per-tool timeout takes precedence over the global default."""
    result = resolve_tool_timeout(
        tool_name="my_tool",
        tools_config=global_tools_config,
        per_tool_timeout=120,
    )
    # Per-tool override (120) must win over global (60).
    # Failure means the function ignores the per-tool value.
    assert result == 120


def test_resolve_tool_timeout_falls_back_to_global(
    global_tools_config: ToolsConfig,
) -> None:
    """When per-tool timeout is None, the global timeout is returned."""
    result = resolve_tool_timeout(
        tool_name="my_tool",
        tools_config=global_tools_config,
        per_tool_timeout=None,
    )
    # Should fall back to global_tools_config.timeout (60).
    # Failure means the function does not honour the global default.
    assert result == 60


# -- resolve_tool_retry --


def test_resolve_tool_retry_uses_per_tool_override(
    global_tools_config: ToolsConfig,
) -> None:
    """Per-tool retry config takes precedence over the global default."""
    per_tool = RetryPolicy(max_retries=5, backoff_base_s=3.0, backoff_max_s=60.0)
    result = resolve_tool_retry(
        tool_name="my_tool",
        tools_config=global_tools_config,
        per_tool_retry=per_tool,
    )
    # The returned config must be the per-tool override, not the global.
    # Failure means the function ignores the per-tool retry config.
    assert result is per_tool
    assert result.max_retries == 5


def test_resolve_tool_retry_falls_back_to_global(
    global_tools_config: ToolsConfig,
) -> None:
    """When per-tool retry is None, the global retry config is returned."""
    result = resolve_tool_retry(
        tool_name="my_tool",
        tools_config=global_tools_config,
        per_tool_retry=None,
    )
    # Should fall back to global_tools_config.retry.
    # Failure means the function does not honour the global default.
    assert result is global_tools_config.retry
    assert result.max_retries == 2


# -- call_tool_with_timeout --


def test_call_tool_with_timeout_succeeds() -> None:
    """A fast tool returns its result within the deadline."""
    result = call_tool_with_timeout(lambda: "ok", timeout=5)
    # The tool finished instantly so the result must be "ok".
    # Failure means the function lost the return value or raised unexpectedly.
    assert result == "ok"


def test_call_tool_with_timeout_raises_on_slow_tool() -> None:
    """A tool that exceeds its deadline raises TimeoutError."""

    def slow_tool() -> str:
        """Simulates a tool that takes longer than the allowed timeout."""
        time.sleep(2)
        return "late"

    # timeout=1 is shorter than the 2s sleep inside slow_tool.
    # Failure means the timeout enforcement is broken.
    with pytest.raises(TimeoutError, match="timed out"):
        call_tool_with_timeout(slow_tool, timeout=1)


# -- execute_tool_with_retry --


def test_execute_tool_with_retry_success_first_attempt(
    on_event: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """When the tool succeeds on the first attempt, no retry events are emitted."""
    retry_config = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_max_s=10.0)
    result = execute_tool_with_retry(
        tool_name="fast_tool",
        call_fn=lambda: "result",
        timeout=5,
        retry_config=retry_config,
        on_event=on_event,
    )
    # Tool succeeded immediately so we get the result back.
    # Failure means the function swallowed the result or retried unnecessarily.
    assert result == "result"
    # No retry or error events should have been emitted on a first-attempt success.
    # Failure means the function emits spurious events.
    retry_events = [e for e in captured_events if e["type"] == "response.retry"]
    assert len(retry_events) == 0


def test_execute_tool_with_retry_retries_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    on_event: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """A timeout on the first attempt triggers a retry that then succeeds."""
    # Patch time.sleep inside the retry module to avoid real delays.
    monkeypatch.setattr("omnigent.runtime.tool_retry.time.sleep", lambda _: None)

    call_count = 0

    def flaky_tool() -> str:
        """
        Fails with TimeoutError on the first call, succeeds on the second.
        """
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("Tool execution timed out after 5s")
        return "ok"

    retry_config = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_max_s=10.0)

    # Patch call_tool_with_timeout so we control failures without real threads.
    monkeypatch.setattr(
        "omnigent.runtime.tool_retry.call_tool_with_timeout",
        lambda call_fn, timeout, cancel_fn=None: flaky_tool(),
    )

    result = execute_tool_with_retry(
        tool_name="flaky_tool",
        call_fn=lambda: "unused",
        timeout=5,
        retry_config=retry_config,
        on_event=on_event,
    )
    # Second attempt succeeded so the result must be "ok".
    # Failure means the retry loop did not re-invoke the tool.
    assert result == "ok"

    # Exactly one retry event should have been emitted (for the first failure).
    # Failure means the function either skipped the retry event or emitted too many.
    retry_events = [e for e in captured_events if e["type"] == "response.retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["tool_name"] == "flaky_tool"


def test_execute_tool_with_retry_returns_error_string_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    on_event: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """When all attempts time out, an error string is returned (not raised)."""
    # Patch time.sleep inside the retry module to avoid real delays.
    monkeypatch.setattr("omnigent.runtime.tool_retry.time.sleep", lambda _: None)

    retry_config = RetryPolicy(max_retries=2, backoff_base_s=1.0, backoff_max_s=10.0)

    # Patch call_tool_with_timeout so every call raises TimeoutError.
    monkeypatch.setattr(
        "omnigent.runtime.tool_retry.call_tool_with_timeout",
        lambda call_fn, timeout, cancel_fn=None: (_ for _ in ()).throw(
            TimeoutError("Tool execution timed out after 5s")
        ),
    )

    result = execute_tool_with_retry(
        tool_name="stuck_tool",
        call_fn=lambda: "unused",
        timeout=5,
        retry_config=retry_config,
        on_event=on_event,
    )
    # The function must return an error string, not raise an exception.
    # Failure means the function lets TimeoutError propagate to the caller.
    assert isinstance(result, str)
    assert "Error" in result
    # 3 = max_retries=2 (retries) + 1 initial attempt. The message
    # reports total tries, not just the retry count.
    assert "3 attempts" in result

    # A response.error event must have been emitted for the terminal failure.
    # Failure means the caller would not be notified of the exhaustion.
    error_events = [e for e in captured_events if e["type"] == "response.error"]
    assert len(error_events) == 1
    assert error_events[0]["tool_name"] == "stuck_tool"


# -- SSE event structure validation --


def test_tool_retry_event_has_correct_fields(
    monkeypatch: pytest.MonkeyPatch,
    on_event: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """
    When a tool times out and retries, the emitted retry event
    contains all required fields with correct types and values.

    :param monkeypatch: Pytest monkeypatch fixture for patching.
    :param on_event: Mock callback that records emitted events.
    :param captured_events: List accumulating events for inspection.
    """
    # Patch time.sleep to avoid real delays.
    monkeypatch.setattr("omnigent.runtime.tool_retry.time.sleep", lambda _: None)
    # Fix random.uniform to 1.0 so delay is deterministic.
    # ``compute_backoff_delay`` lives on ``RetryPolicy`` (in
    # ``omnigent.spec.types``) and uses a function-local
    # ``import random`` — patching either
    # ``omnigent.runtime.tool_retry.random`` or
    # ``omnigent.spec.types.random`` won't catch the local
    # binding. Patch the global ``random.uniform`` instead.
    import random as _random_module

    monkeypatch.setattr(_random_module, "uniform", lambda _low, _high: 1.0)

    call_count = 0

    def timeout_then_succeed(
        call_fn: Callable[[], str],
        timeout: int,
        cancel_fn: Callable[[], None] | None = None,
    ) -> str:
        """
        Raises TimeoutError on first call, returns success on second.

        :param call_fn: The tool callable (ignored in this stub).
        :param timeout: The timeout value (ignored in this stub).
        :param cancel_fn: The cancel callback (ignored in this stub).
        :returns: ``"ok"`` on second call.
        :raises TimeoutError: On first call.
        """
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("Tool execution timed out after 5s")
        return "ok"

    monkeypatch.setattr(
        "omnigent.runtime.tool_retry.call_tool_with_timeout",
        timeout_then_succeed,
    )

    retry_config = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_max_s=10.0)
    result = execute_tool_with_retry(
        tool_name="my_tool",
        call_fn=lambda: "unused",
        timeout=5,
        retry_config=retry_config,
        on_event=on_event,
    )

    # Tool recovered on second attempt so result must be "ok".
    # Failure means the retry loop did not succeed after recovery.
    assert result == "ok"

    retry_events = [e for e in captured_events if e["type"] == "response.retry"]
    # Exactly one retry event for the single timeout failure.
    # Failure means the function emitted too many or too few retry events.
    assert len(retry_events) == 1

    evt = retry_events[0]

    # "type" must be "response.retry" — identifies this as a retry SSE event.
    # Failure means the event type is wrong, breaking SSE consumers.
    assert evt["type"] == "response.retry"

    # "source" must be "tool" — distinguishes tool retries from other sources.
    # Failure means the event source label is incorrect.
    assert evt["source"] == "tool"

    # "tool_name" must match the tool that was retried.
    # Failure means the event references the wrong tool.
    assert evt["tool_name"] == "my_tool"

    # "attempt" is the 1-based NEXT attempt number: attempt=0 (zero-based)
    # produces attempt + 2 = 2 in the event.
    # Failure means attempt numbering logic is wrong.
    assert evt["attempt"] == 2

    # "max_attempts" reports total tries (max_retries=3 means 4
    # total attempts: 1 initial + 3 retries). The wire format
    # was renamed during the RetryConfig -> RetryPolicy migration
    # but reports total tries to match user-facing semantics
    # (a "max attempts" budget is more intuitive than a "retries
    # beyond initial" count).
    assert evt["max_attempts"] == 4

    # "delay_seconds" with backoff_base_s=1.0, attempt=0, random=1.0:
    # min(1.0 ** 0, 10.0) * 1.0 = 1.0, rounded to 2 decimals = 1.0.
    # Failure means the backoff calculation or rounding is wrong.
    assert evt["delay_seconds"] == 1.0
    assert isinstance(evt["delay_seconds"], float)

    # "error.code" must be "timeout" for timeout-triggered retries.
    # Failure means the error classification is wrong.
    assert evt["error"]["code"] == "timeout"

    # "error.message" must contain "timed out" from the TimeoutError.
    # Failure means the original error message was lost or altered.
    assert "timed out" in evt["error"]["message"]

    # "error.detail" must be None — no additional detail for timeouts.
    # Failure means unexpected detail was attached.
    assert evt["error"]["detail"] is None


def test_tool_error_event_has_correct_fields(
    monkeypatch: pytest.MonkeyPatch,
    on_event: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """
    When all retries are exhausted, the emitted error event contains
    all required fields with correct types and values.

    :param monkeypatch: Pytest monkeypatch fixture for patching.
    :param on_event: Mock callback that records emitted events.
    :param captured_events: List accumulating events for inspection.
    """
    # Patch time.sleep to avoid real delays.
    monkeypatch.setattr("omnigent.runtime.tool_retry.time.sleep", lambda _: None)

    # Every call raises TimeoutError to exhaust all attempts.
    monkeypatch.setattr(
        "omnigent.runtime.tool_retry.call_tool_with_timeout",
        lambda call_fn, timeout, cancel_fn=None: (_ for _ in ()).throw(
            TimeoutError("Tool execution timed out after 5s")
        ),
    )

    retry_config = RetryPolicy(max_retries=2, backoff_base_s=1.0, backoff_max_s=10.0)
    execute_tool_with_retry(
        tool_name="stuck_tool",
        call_fn=lambda: "unused",
        timeout=5,
        retry_config=retry_config,
        on_event=on_event,
    )

    error_events = [e for e in captured_events if e["type"] == "response.error"]
    # Exactly one terminal error event after exhaustion.
    # Failure means the function emitted multiple error events or none.
    assert len(error_events) == 1

    evt = error_events[0]

    # "type" must be "response.error" — identifies this as a terminal
    # error SSE event.
    # Failure means the event type is wrong, breaking SSE consumers.
    assert evt["type"] == "response.error"

    # "source" must be "tool" — distinguishes tool errors from others.
    # Failure means the event source label is incorrect.
    assert evt["source"] == "tool"

    # "tool_name" must match the tool that failed.
    # Failure means the event references the wrong tool.
    assert evt["tool_name"] == "stuck_tool"

    # "error.code" must be "timeout" for timeout-based exhaustion.
    # Failure means the error classification is wrong.
    assert evt["error"]["code"] == "timeout"

    # "error.message" must contain "attempts exhausted" to indicate
    # terminal failure, and include the attempt count.
    # Failure means the exhaustion message format changed.
    assert "attempts exhausted" in evt["error"]["message"]
    # 3 = max_retries=2 (retries) + 1 initial attempt. The message
    # reports total tries, not just the retry count.
    assert "3 attempts" in evt["error"]["message"]

    # "error.detail" must be None — no additional detail for timeouts.
    # Failure means unexpected detail was attached.
    assert evt["error"]["detail"] is None


def test_tool_retry_non_timeout_exception_no_retry(
    monkeypatch: pytest.MonkeyPatch,
    on_event: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """
    When a tool raises a non-timeout exception, the retry loop breaks
    immediately and emits only a terminal error event (no retries).

    :param monkeypatch: Pytest monkeypatch fixture for patching.
    :param on_event: Mock callback that records emitted events.
    :param captured_events: List accumulating events for inspection.
    """

    def raise_value_error(
        call_fn: Callable[[], str],
        timeout: int,
        cancel_fn: Callable[[], None] | None = None,
    ) -> str:
        """
        Always raises ValueError to simulate a non-timeout failure.

        :param call_fn: The tool callable (ignored in this stub).
        :param timeout: The timeout value (ignored in this stub).
        :param cancel_fn: The cancel callback (ignored in this stub).
        :raises ValueError: Always.
        """
        raise ValueError("bad input")

    monkeypatch.setattr(
        "omnigent.runtime.tool_retry.call_tool_with_timeout",
        raise_value_error,
    )

    retry_config = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_max_s=10.0)
    result = execute_tool_with_retry(
        tool_name="broken_tool",
        call_fn=lambda: "unused",
        timeout=5,
        retry_config=retry_config,
        on_event=on_event,
    )

    # No retry events should be emitted for non-timeout exceptions.
    # Failure means the function retried when it should have broken out.
    retry_events = [e for e in captured_events if e["type"] == "response.retry"]
    assert len(retry_events) == 0

    # Exactly one terminal error event for the immediate failure.
    # Failure means the function did not emit the error event.
    error_events = [e for e in captured_events if e["type"] == "response.error"]
    assert len(error_events) == 1

    evt = error_events[0]

    # "type" must be "response.error" — terminal failure event.
    assert evt["type"] == "response.error"

    # "source" must be "tool".
    assert evt["source"] == "tool"

    # "tool_name" must match the tool that failed.
    assert evt["tool_name"] == "broken_tool"

    # "error.message" must contain "ValueError" so the error type is
    # visible to the caller, and "attempts exhausted" for the terminal
    # format.
    # Failure means the original exception type was lost.
    assert "ValueError" in evt["error"]["message"]
    assert "attempts exhausted" in evt["error"]["message"]

    # "error.detail" must be None.
    assert evt["error"]["detail"] is None

    # The return value must be an error string (not raised).
    # Failure means the function let the ValueError propagate.
    assert isinstance(result, str)
    assert "ValueError" in result
