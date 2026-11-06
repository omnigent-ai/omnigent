"""E2E regression test: claude-sdk surfaces a useful reason on an empty-result failure.

The Claude Agent SDK ends a turn with a ``ResultMessage``. When that message
carries ``is_error=True`` the claude-sdk executor turns it into an
``ExecutorError`` whose ``message`` is the turn's failure reason (the runner then
shows it to the user, wrapped as ``inner executor error: <reason>``). For an
API-level failure (a 401/403/404, or a mid-stream truncation) the SDK populates
``ResultMessage.result`` with a descriptive ``API Error: ...`` string, so the
user sees a useful cause. But for the *empty-result* failure subtypes —
``error_during_execution`` (an abort mid-turn) and ``error_max_turns`` — the SDK
leaves ``result`` empty while still setting ``is_error=True``. The executor does::

    failure_text = result_msg.result or "claude-sdk harness error"

so on an empty ``result`` the user is handed the bare, detail-free literal
``"claude-sdk harness error"`` — even though ``result_msg.subtype`` (e.g.
``error_during_execution``), ``system_diagnostics``, and captured ``stderr`` all
hold the real cause. That is the bug: a turn that fails with no actionable
reason.

User journey reproduced here:

1. A claude-sdk agent turn is running and streaming.
2. Mid-turn the run is aborted (production hits this via an internal/watchdog
   abort or an orchestrator interrupt that is *not* a clean user-cancel). The
   SDK emits ``ResultMessage(is_error=True, subtype="error_during_execution",
   result=None)``.
3. The turn fails and the surfaced reason is the detail-free
   ``"claude-sdk harness error"`` instead of anything derived from the subtype /
   diagnostics.

This drives the **real** journey against the real ``claude`` CLI subprocess (the
same binary the runner spawns) through the real ``ClaudeSDKExecutor``, pointed at
the mock gateway. We start a slow-streamed turn, wait until it is actually
streaming, then interrupt the live SDK client mid-stream to force the exact
``error_during_execution`` / ``result=None`` ``ResultMessage`` production hits
(the interrupt stands in for the internal abort — the outcome signature is
identical). We assert on the reason the executor surfaces.

The runner's own user-cancel path deliberately masks this (its
``interrupt_session`` closes the session, coalescing the abort into a clean
``TurnComplete``), so the empty-result reason is only observable at the executor
boundary — hence an executor-level e2e rather than a UI-driven one.

Fail→pass target for the fix: today the surfaced reason is exactly
``"claude-sdk harness error"`` (bug present → this test FAILS). Once the fix
preserves the subtype / diagnostics when ``result`` is empty, the reason is no
longer the bare literal and this test PASSES — while the reproduction
precondition (a mid-stream abort was driven and surfaced a turn-failure reason)
still holds, so a fix must change the *reason*, not merely dodge the abort.

Usage::

    OMNIGENT_CLAUDE_SDK_NO_SANDBOX=1 uv run --group test pytest \\
      tests/e2e/test_claude_sdk_empty_error_reason_e2e.py -v --timeout=180
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass, field

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TextChunk
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

# A plain (non-gateway) Anthropic model id; the mock's ``default`` queue serves
# any model, so the exact spelling only needs to be a valid Claude id.
_MODEL = "claude-sonnet-4-20250514"

# The exact detail-free literal the executor falls back to when a failing
# ``ResultMessage`` has an empty ``result`` (claude_sdk_executor.py). This is the
# useless reason the bug surfaces; the fix must replace it with something derived
# from the subtype / diagnostics.
_USELESS_REASON = "claude-sdk harness error"

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason=(
        "Real-CLI e2e: requires the ``claude`` binary on PATH (the runner spawns "
        "it). Install the Claude Code CLI and re-run."
    ),
)


@dataclass
class _TurnOutcome:
    """What one interrupted turn produced, for both the bug assertion and the
    reproduction-precondition assertions."""

    surfaced_error: str | None = None
    saw_text: bool = False
    interrupt_issued: bool = False
    events: list[str] = field(default_factory=list)


async def _drive_turn_and_abort_midstream(
    executor: ClaudeSDKExecutor, session_key: str
) -> _TurnOutcome:
    """Run one turn; once text is streaming, interrupt the live SDK client.

    A bare ``client.interrupt()`` (no session close) makes the SDK end the turn
    with ``ResultMessage(is_error=True, subtype="error_during_execution",
    result=None)`` — the empty-result failure the bug mishandles. The returned
    outcome carries the surfaced ``ExecutorError.message`` plus proof the
    intended path ran (text streamed, interrupt issued).
    """
    messages = [{"role": "user", "content": "Tell me a long story.", "session_id": session_key}]
    outcome = _TurnOutcome()

    async def _interrupt_when_streaming() -> None:
        # Poll until the live client exists AND text has begun flowing, so the
        # interrupt lands mid-stream (before the completion event) rather than
        # racing session setup or arriving after the turn already finished.
        for _ in range(400):
            client_state = executor._clients.get(session_key)
            if client_state is not None and outcome.saw_text:
                with contextlib.suppress(Exception):
                    await client_state.client.interrupt()
                outcome.interrupt_issued = True
                return
            await asyncio.sleep(0.02)

    interrupt_task = asyncio.ensure_future(_interrupt_when_streaming())
    try:
        async for event in executor.run_turn(
            messages=messages,
            tools=[],
            system_prompt="You are terse.",
            config=ExecutorConfig(model=_MODEL),
        ):
            outcome.events.append(type(event).__name__)
            if isinstance(event, TextChunk):
                outcome.saw_text = True
            elif isinstance(event, ExecutorError):
                outcome.surfaced_error = event.message
    finally:
        interrupt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await interrupt_task
        with contextlib.suppress(Exception):
            await executor.close_session(session_key)
    return outcome


def test_claude_sdk_empty_error_result_surfaces_useful_reason(
    isolated_mock_llm_server_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``is_error`` turn with an empty ``result`` must not surface a bare, detail-free reason.

    Drives a real ``claude`` CLI turn through the real ``ClaudeSDKExecutor``,
    forces an ``error_during_execution`` / ``result=None`` ``ResultMessage`` by
    interrupting the live SDK client mid-stream, and asserts the surfaced failure
    reason carries actual cause information instead of the fallback literal
    ``"claude-sdk harness error"``.
    """
    # Point the real claude CLI at the mock gateway and disable the bwrap sandbox
    # (it cannot create namespaces in CI/nested sandboxes; the ResultMessage
    # handling under test is identical with or without it).
    monkeypatch.setenv("ANTHROPIC_BASE_URL", isolated_mock_llm_server_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-key")
    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")

    reset_mock_llm(isolated_mock_llm_server_url)
    # A slowly-streamed long answer: many word deltas paced by ``chunk_delay`` so
    # the mid-stream interrupt reliably lands before the completion event.
    # Repeated so any CLI-side resume/retry keeps streaming rather than finishing.
    long_text = " ".join(["word"] * 80)
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [{"text": long_text, "chunk_delay": 0.25}] * 4,
        key="default",
    )

    executor = ClaudeSDKExecutor(
        model=_MODEL,
        permission_mode="bypassPermissions",
        api_key_helper="printf mock-key",
    )
    outcome = asyncio.run(
        _drive_turn_and_abort_midstream(executor, session_key="empty-error-reason-repro")
    )

    # Reproduction preconditions: the intended path ran — the turn actually
    # streamed, we issued the mid-stream interrupt, and it surfaced a turn-failure
    # reason (the ``is_error`` ResultMessage path). If any of these is false the
    # harness did not exercise the empty-result failure and the bug assertion
    # below would be meaningless.
    assert outcome.saw_text, (
        f"the turn never streamed text, so the interrupt could not land mid-stream; "
        f"events seen: {outcome.events}"
    )
    assert outcome.interrupt_issued, (
        "the mid-stream interrupt was never issued (the live SDK client did not "
        f"appear while streaming); events seen: {outcome.events}"
    )
    assert outcome.surfaced_error is not None, (
        "expected the mid-turn abort to surface an ExecutorError (the is_error "
        f"ResultMessage path); got none. events seen: {outcome.events}"
    )

    # The bug: on an empty ``ResultMessage.result`` the executor surfaces the bare
    # fallback literal, discarding subtype/diagnostics. The fix must surface a
    # reason derived from the real cause (subtype such as error_during_execution,
    # system_diagnostics, or captured stderr) instead.
    assert outcome.surfaced_error != _USELESS_REASON, (
        "claude-sdk surfaced its detail-free fallback reason "
        f"{_USELESS_REASON!r} for an is_error turn with an empty result, "
        "discarding the ResultMessage subtype (error_during_execution) and "
        f"diagnostics. Surfaced reason was: {outcome.surfaced_error!r}"
    )
