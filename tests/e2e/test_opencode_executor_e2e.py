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
from pathlib import Path

import pytest

from omnigent.inner.executor import (
    ExecutorError,
    ExecutorEvent,
    TextChunk,
    TurnComplete,
)
from omnigent.inner.opencode_executor import OpenCodeExecutor

_GATE_ENV = "OMNIGENT_E2E_OPENCODE"
_TIMEOUT_ENV = "OMNIGENT_E2E_OPENCODE_TIMEOUT"
_RESUME_MARKER = "amber-lantern-42"
_DEFAULT_TIMEOUT_SECONDS = 120.0
_SKIP_REASON = (
    f"opencode e2e needs `opencode` on PATH and {_GATE_ENV}=1 to run (costs real provider tokens)"
)


def _e2e_opted_in() -> bool:
    """Return whether the explicit real-token gate is open."""
    return os.environ.get(_GATE_ENV) == "1"


_E2E_OPT_IN_MARK = pytest.mark.skipif(not _e2e_opted_in(), reason=_SKIP_REASON)
_REAL_E2E_MARK = pytest.mark.skipif(
    not _e2e_opted_in() or shutil.which("opencode") is None,
    reason=_SKIP_REASON,
)


def _turn_timeout_seconds() -> float:
    """Return the real-provider timeout, rejecting invalid overrides."""
    raw = os.environ.get(_TIMEOUT_ENV, str(_DEFAULT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise AssertionError(f"{_TIMEOUT_ENV} must be a positive number") from exc
    if value <= 0:
        raise AssertionError(f"{_TIMEOUT_ENV} must be a positive number")
    return value


@pytest.fixture
def _real_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OpenCodeExecutor:
    """Keep provider context small and deterministic for real CLI smoke tests."""
    monkeypatch.setenv("HARNESS_OPENCODE_CWD", str(tmp_path))
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp_root))
    return OpenCodeExecutor()


@_E2E_OPT_IN_MARK
def test_opted_in_e2e_requires_opencode_binary() -> None:
    """Explicit opt-in fails loudly when the real CLI is unavailable."""
    binary = shutil.which("opencode")
    assert binary is not None, (
        f"{_GATE_ENV}=1 but the opencode binary is missing from PATH; "
        "install OpenCode or unset the opt-in gate"
    )


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
    try:
        async with asyncio.timeout(_turn_timeout_seconds()):
            async for event in stream:
                events.append(event)
    except TimeoutError as exc:
        raise AssertionError(
            f"OpenCode produced no complete turn within {_turn_timeout_seconds():.0f}s; "
            f"increase {_TIMEOUT_ENV} only when the provider is known to be slow"
        ) from exc
    return events


@_REAL_E2E_MARK
def test_opencode_run_turn_streams_text_against_real_binary(
    _real_executor: OpenCodeExecutor,
) -> None:
    """One-shot turn returns a non-empty :class:`TurnComplete`.

    Locks in the JSONL parser against whatever event shapes the
    installed ``opencode`` version actually emits. A shape drift in
    upstream — a renamed ``"text"`` event type, a moved ``part.text``
    field — fails here loud rather than silently degrading to empty
    assistant turns.

    Skipped without ``OMNIGENT_E2E_OPENCODE=1`` and the binary.
    """

    async def _run() -> list[ExecutorEvent]:
        try:
            return await _collect_turn(
                _real_executor,
                prompt=(
                    "Reply with exactly the single word 'pong' and nothing else. "
                    "Do not call any tools."
                ),
                session_key="e2e_oneshot",
            )
        finally:
            await _real_executor.close_session("e2e_oneshot")

    events = asyncio.run(_run())

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


@_REAL_E2E_MARK
def test_opencode_run_turn_session_resume_carries_history(
    _real_executor: OpenCodeExecutor,
) -> None:
    """Second turn on the same ``session_key`` recalls the first.

    The executor captures the OpenCode ``sessionID`` from the first
    turn's event stream and reuses it via ``--session <id>`` on
    subsequent turns. Without that wiring, the second model call would
    have no memory of the first and answer with a generic refusal /
    confusion. Proves session resume end-to-end against the real
    binary.

    Skipped without ``OMNIGENT_E2E_OPENCODE=1`` and the binary.
    """

    async def _run_resume() -> list[ExecutorEvent]:
        try:
            await _collect_turn(
                _real_executor,
                prompt=(
                    f"For this harmless continuity test, the reference label is {_RESUME_MARKER}. "
                    "Reply with just OK; do not call any tools."
                ),
                session_key="e2e_resume",
            )
            # Captured OpenCode session id must be cached against our key.
            assert "e2e_resume" in _real_executor._session_ids, (
                "executor did not capture an OpenCode session id from the first turn"
            )
            return await _collect_turn(
                _real_executor,
                prompt=(
                    "What reference label did I provide in the previous turn? "
                    "Reply with only the exact label; do not call any tools."
                ),
                session_key="e2e_resume",
            )
        finally:
            await _real_executor.close_session("e2e_resume")

    second = asyncio.run(_run_resume())
    text = "".join(e.text for e in second if isinstance(e, TextChunk)).lower()
    assert _RESUME_MARKER in text, (
        "second turn did not recall the first turn's content — "
        f"session resume likely broken. Got: {text!r}"
    )
