"""
Fixture harness for executor-adapter tests.

Constructs a :class:`ExecutorAdapter` wrapped around a
:class:`MockExecutor` whose script is selected by the
``MOCK_EXECUTOR_SCRIPT`` env var the tests set per case.

Four scripts:

- ``"text_only"``: a single TurnComplete with response text.
- ``"tool_call"``: a ToolCallRequest, then a ToolCallComplete
  with a result, then a TurnComplete (no further text).
- ``"error"``: an ExecutorError event.
- ``"notice"``: a TurnNotice followed by a clean TurnComplete.
- ``"cancelled"``: a TurnCancelled event.
- ``"capture_messages"``: writes the received messages list as
  JSON to the path in ``MOCK_EXECUTOR_CAPTURE_PATH``, then
  emits a TurnComplete. Used to verify the full AP→harness→
  inner-executor history pipeline (the regression that broke
  ``--resume`` follow-ups).

Lives under ``tests/`` so it doesn't ship as production code.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable

from fastapi import FastAPI

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    MockExecutor,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnCancelled,
    TurnComplete,
    TurnNotice,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_SCRIPT_ENV_VAR = "MOCK_EXECUTOR_SCRIPT"
_CAPTURE_PATH_ENV_VAR = "MOCK_EXECUTOR_CAPTURE_PATH"


class _CapturingExecutor(Executor):
    """
    Inner :class:`Executor` that writes the messages it receives
    to a file the test reads back.

    Used to verify the harness boundary preserves the full
    conversation history (the bug fixed in the resume-history
    commit). The capture file is the proof: if the file shows
    only the most recent user message, the harness regressed
    to "latest user only" and ``--resume`` follow-ups will
    silently lose context again.
    """

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        capture_path = os.environ.get(_CAPTURE_PATH_ENV_VAR)
        if capture_path:
            with open(capture_path, "w", encoding="utf-8") as f:
                json.dump(messages, f)
        yield TurnComplete(response="captured")

    async def close(self) -> None:
        """No-op — no resources to release in the capture stub."""

    async def close_session(self, session_key: str) -> None:
        """No-op — no per-session resources to release."""
        del session_key


def _build_text_only() -> Executor:
    """
    MockExecutor scripted with a single text-only TurnComplete.

    :returns: A configured :class:`MockExecutor` instance.
    """
    executor = MockExecutor()
    executor.enqueue_response("hello from mock")
    return executor


def _build_tool_call() -> Executor:
    """
    MockExecutor scripted with a tool call observation.

    Yields a :class:`ToolCallRequest`, a :class:`ToolCallComplete`
    with a string result, then a final :class:`TurnComplete`. The
    adapter should translate request+complete into paired
    function_call + function_call_output items per the v1
    native-tool emission pattern.

    :returns: A configured :class:`MockExecutor` instance.
    """
    executor = MockExecutor()
    # Hand-build the events list — MockExecutor's enqueue_tool_call
    # helper splits the tool call across two turns to simulate the
    # external-loop pattern, but we want a single turn that emits
    # request + complete + turn-complete in sequence (which is
    # what handles_tools_internally executors do).
    executor._turns.append(
        [
            ToolCallRequest(
                name="echo_tool",
                args={"x": 1},
                metadata={"call_id": "call_test_1"},
            ),
            ToolCallComplete(
                name="echo_tool",
                status=ToolCallStatus.SUCCESS,
                result="tool result",
                # A handles_tools_internally executor (e.g. antigravity) stamps
                # the request's real call_id on the completion too, so the
                # observed function_call and its function_call_output pair
                # downstream (an id-less completion cannot pair and is dropped).
                metadata={"call_id": "call_test_1"},
            ),
            TurnComplete(response=None),
        ]
    )
    return executor


def _build_error() -> Executor:
    """
    MockExecutor scripted with an :class:`ExecutorError`.

    The adapter should re-raise this so the scaffold emits
    ``response.failed``.

    :returns: A configured :class:`MockExecutor` instance.
    """
    executor = MockExecutor()
    executor._turns.append([ExecutorError(message="mock error")])
    return executor


def _build_error_with_usage() -> Executor:
    """
    MockExecutor scripted with an :class:`ExecutorError` carrying usage.

    Mirrors a turn that failed after the model call started: the executor
    observed the prompt size before dying, so the error carries it and the
    scaffold's ``response.failed`` terminal event must surface it (else the
    context-occupancy meter freezes at the previous turn's value).

    :returns: A configured :class:`MockExecutor` instance.
    """
    executor = MockExecutor()
    executor._turns.append(
        [
            ExecutorError(
                message="mock error after usage observed",
                usage={
                    "input_tokens": 400,
                    "output_tokens": 0,
                    "total_tokens": 400,
                    "context_tokens": 100_000,
                    "model": "mock-model",
                },
            )
        ]
    )
    return executor


def _build_notice() -> Executor:
    """
    MockExecutor scripted with a :class:`TurnNotice` + clean completion.

    Mirrors a native executor answering an intercepted command with
    guidance: the adapter must surface the notice as an info-level error
    item and let the turn end with ``response.completed`` — never
    ``response.failed``.

    :returns: A configured :class:`MockExecutor` instance.
    """
    executor = MockExecutor()
    executor._turns.append(
        [
            TurnNotice(
                message="Run omni setup on the host to sign in again.",
                code="mock_notice",
            ),
            TurnComplete(response=None),
        ]
    )
    return executor


def _build_cancelled() -> Executor:
    """
    MockExecutor scripted with a provider-side :class:`TurnCancelled`.

    The adapter should map this cleanly to ``response.cancelled`` rather than
    falling through to ``response.completed``.

    :returns: A configured :class:`MockExecutor` instance.
    """
    executor = MockExecutor()
    executor._turns.append([TurnCancelled(reason="provider_cancelled")])
    return executor


def _build_capture_messages() -> Executor:
    """
    :class:`_CapturingExecutor` builder.

    The executor itself reads ``MOCK_EXECUTOR_CAPTURE_PATH``
    inside ``run_turn`` so the test can drop the path in just
    before each request.

    :returns: A :class:`_CapturingExecutor` instance.
    """
    return _CapturingExecutor()


def _build_pr_tracking() -> Executor:
    executor = MockExecutor()
    executor._turns.append(
        [
            ToolCallRequest(
                name="Bash", args={"command": "gh pr create"}, metadata={"call_id": "pr1"}
            ),
            ToolCallComplete(
                name="Bash",
                status=ToolCallStatus.SUCCESS,
                result="https://github.com/example/sdk/pull/42",
                metadata={"call_id": "pr1"},
            ),
            TurnComplete(response="Created"),
        ]
    )
    return executor


_SCRIPTS: dict[str, Callable[[], Executor]] = {
    "pr_tracking": _build_pr_tracking,
    "text_only": _build_text_only,
    "tool_call": _build_tool_call,
    "error": _build_error,
    "error_with_usage": _build_error_with_usage,
    "notice": _build_notice,
    "cancelled": _build_cancelled,
    "capture_messages": _build_capture_messages,
}


def create_app() -> FastAPI:
    """
    Build the fixture FastAPI app for whichever script the
    ``MOCK_EXECUTOR_SCRIPT`` env var selects.

    :returns: The :class:`ExecutorAdapter`'s
        :class:`FastAPI` instance.
    :raises ValueError: If the env var is unset or names an
        unknown script.
    """
    script_name = os.environ.get(_SCRIPT_ENV_VAR)
    if script_name is None:
        raise ValueError(
            f"{_SCRIPT_ENV_VAR} env var not set; tests must select "
            f"a script before spawning the runner"
        )
    builder = _SCRIPTS.get(script_name)
    if builder is None:
        raise ValueError(f"unknown mock script {script_name!r}; available: {sorted(_SCRIPTS)}")
    adapter = ExecutorAdapter(executor_factory=builder)
    return adapter.build()
