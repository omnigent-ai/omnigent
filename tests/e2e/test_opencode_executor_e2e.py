"""End-to-end smoke for the OpenCode inner executor.

Runs a real ``opencode run --format json`` subprocess and asserts the
event stream parses into the inner :mod:`omnigent.inner.executor`
vocabulary. Gated on both ``OMNIGENT_E2E_OPENCODE=1`` and the
``opencode`` binary being on ``PATH``, so a default ``pytest`` run on
a machine without OpenCode installed skips silently.

These tests cost real provider tokens. The opt-in env var prevents CI
boxes that happen to install opencode from burning credits without
explicit intent. Mirror of the gating pattern in
``tests/e2e/test_host_codex_native_e2e.py``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator

import pytest

from omnigent.inner.executor import (
    ExecutorError,
    ExecutorEvent,
    TextChunk,
    TurnComplete,
)
from omnigent.inner.opencode_executor import OpenCodeExecutor

_GATE_ENV = "OMNIGENT_E2E_OPENCODE"
_SKIP_REASON = (
    f"opencode e2e needs `opencode` on PATH and {_GATE_ENV}=1 to run (costs real provider tokens)"
)


def _e2e_enabled() -> bool:
    """Return whether the opencode e2e gate is open.

    Two conditions: the opt-in env var is set, and the CLI exists on
    ``PATH``. Either alone is insufficient — the env var alone risks
    a confusing error on a machine without opencode, and the binary
    alone risks spending tokens during a default ``pytest`` run.
    """
    return os.environ.get(_GATE_ENV) == "1" and shutil.which("opencode") is not None


pytestmark = pytest.mark.skipif(not _e2e_enabled(), reason=_SKIP_REASON)


async def _collect_turn(
    executor: OpenCodeExecutor,
    prompt: str,
    session_key: str = "e2e_session",
) -> list[ExecutorEvent]:
    """Drive one ``run_turn`` to completion and return every yielded event.

    Threads ``session_key`` as the Omnigent ``session_id`` on the
    user message so the executor's per-Omnigent-session OpenCode-id
    cache picks it up — this is how multi-turn resume is exercised
    in the second test.
    """
    events: list[ExecutorEvent] = []
    stream: AsyncIterator[ExecutorEvent] = executor.run_turn(
        messages=[
            {
                "role": "user",
                "content": prompt,
                "session_id": session_key,
            }
        ],
        tools=[],
        system_prompt="",
        config=None,
    )
    async for event in stream:
        events.append(event)
    return events


def test_opencode_run_turn_streams_text_against_real_binary() -> None:
    """One-shot turn returns a non-empty :class:`TurnComplete`.

    Locks in the JSONL parser against whatever event shapes the
    installed ``opencode`` version actually emits. A shape drift in
    upstream — a renamed ``"text"`` event type, a moved ``part.text``
    field — fails here loud rather than silently degrading to empty
    assistant turns.

    Skipped without ``OMNIGENT_E2E_OPENCODE=1`` and the binary.
    """
    executor = OpenCodeExecutor()
    events = asyncio.run(
        _collect_turn(
            executor,
            prompt=(
                "Reply with exactly the single word 'pong' and nothing else. "
                "Do not call any tools."
            ),
            session_key="e2e_oneshot",
        )
    )

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert not errors, f"opencode emitted ExecutorError(s): {errors!r}"

    text_events = [e for e in events if isinstance(e, TextChunk)]
    assert text_events, (
        "opencode emitted no TextChunk events — either the model "
        "produced no text or the JSONL parser dropped them on the floor"
    )
    joined = "".join(e.text for e in text_events).lower()
    assert "pong" in joined, f"unexpected reply from opencode: {joined!r}"

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1, (
        f"expected exactly one TurnComplete, got {len(completes)}: {completes!r}"
    )


def test_opencode_run_turn_session_resume_carries_history() -> None:
    """Second turn on the same ``session_key`` recalls the first.

    The executor captures the OpenCode ``sessionID`` from the first
    turn's event stream and reuses it via ``--session <id>`` on
    subsequent turns. Without that wiring, the second model call would
    have no memory of the first and answer with a generic refusal /
    confusion. Proves session resume end-to-end against the real
    binary.

    Skipped without ``OMNIGENT_E2E_OPENCODE=1`` and the binary.
    """
    executor = OpenCodeExecutor()
    asyncio.run(
        _collect_turn(
            executor,
            prompt=(
                "Remember the secret word 'cactus'. Reply with just OK; do not call any tools."
            ),
            session_key="e2e_resume",
        )
    )
    # Captured OpenCode session id must be cached against our key.
    assert "e2e_resume" in executor._session_ids, (
        "executor did not capture an OpenCode session id from the first turn"
    )

    second = asyncio.run(
        _collect_turn(
            executor,
            prompt=(
                "What was the secret word I just told you? "
                "Reply with only the word; do not call any tools."
            ),
            session_key="e2e_resume",
        )
    )
    text = "".join(e.text for e in second if isinstance(e, TextChunk)).lower()
    assert "cactus" in text, (
        "second turn did not recall the first turn's content — "
        f"session resume likely broken. Got: {text!r}"
    )
