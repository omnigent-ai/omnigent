"""End-to-end tests: the headless ``goose`` harness drives ``goose acp``.

The chat-first sibling of ``test_goose_native_cli_e2e``. The ``goose`` harness
runs Block's Goose over the Agent Client Protocol (``goose acp``):
:class:`omnigent.inner.goose_executor.GooseExecutor` spawns the subprocess,
streams ``agent_message_chunk`` updates as chat text, and routes Goose's mid-turn
``session/request_permission`` through Omnigent's TOOL_CALL policy + human-consent
elicitation (the same bridges the runner's ExecutorAdapter installs). This test
drives the executor directly against a *real* ``goose acp`` process and asserts
the full round-trip: streaming text, a tool-call permission surfaced to the
elicitation handler, the tool running on approval, and token usage on completion.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_GOOSE=1`` to run. Needs a configured Goose
  provider — ``GOOSE_PROVIDER`` + ``GOOSE_MODEL`` and the provider's key in the
  environment (e.g. ``ANTHROPIC_API_KEY``) — plus the ``goose`` binary on PATH.
  The test runs in an isolated ``$HOME`` so it never reads or writes the
  developer's real ``~/.config/goose`` (provider comes from env; the per-tool
  grant Goose may persist lands in the temp HOME and is discarded).

    OMNIGENT_E2E_GOOSE=1 \
    GOOSE_PROVIDER=anthropic GOOSE_MODEL=claude-haiku-4-5-20251001 \
    ANTHROPIC_API_KEY=... \
    .venv/bin/python -m pytest tests/e2e/test_goose_acp_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.goose_executor import GooseExecutor

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_GOOSE") != "1"
    or shutil.which("goose") is None
    or not os.environ.get("GOOSE_PROVIDER")
    or not os.environ.get("GOOSE_MODEL"),
    reason=(
        "headless goose ACP e2e is opt-in: set OMNIGENT_E2E_GOOSE=1 with a "
        "configured Goose provider (GOOSE_PROVIDER + GOOSE_MODEL + a provider key "
        "in the env) and the `goose` binary on PATH."
    ),
)


class _AskVerdict:
    """A TOOL_CALL policy verdict that always defers to elicitation."""

    action = "POLICY_ACTION_ASK"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME``/``$XDG_CONFIG_HOME`` at a temp dir for goose isolation.

    Goose resolves its provider from the inherited ``GOOSE_PROVIDER`` env + the
    provider key, so a fresh HOME still authenticates; any ``permission.yaml``
    Goose persists from an ``allow_*`` grant lands here and is discarded.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    # Force per-tool approval so session/request_permission fires.
    monkeypatch.setenv("GOOSE_MODE", "approve")
    return tmp_path


@pytest.mark.asyncio
async def test_goose_acp_streams_and_completes(isolated_home: Path) -> None:
    """A plain prose turn streams agent text and completes with token usage."""
    executor = GooseExecutor(
        provider=os.environ["GOOSE_PROVIDER"],
        model=os.environ["GOOSE_MODEL"],
        builtins=("developer",),
    )
    chunks: list[str] = []
    final: TurnComplete | None = None
    try:
        async for ev in executor.run_turn(
            [{"role": "user", "content": "Reply with exactly the word PONG and nothing else."}],
            tools=[],
            system_prompt="You are a terse assistant.",
        ):
            if isinstance(ev, TextChunk):
                chunks.append(ev.text)
            elif isinstance(ev, TurnComplete):
                final = ev
            elif isinstance(ev, ExecutorError):
                pytest.fail(f"executor error: {ev.message}")
    finally:
        await executor.close()

    assert final is not None, "expected a TurnComplete"
    assert "PONG" in ("".join(chunks) + (final.response or ""))
    # Goose reports usage; the executor maps it onto TurnComplete.usage.
    assert final.usage is not None and final.usage.get("total_tokens", 0) > 0
    # The context window is learned from usage_update during the turn.
    assert executor.max_context_tokens() is not None


@pytest.mark.asyncio
async def test_goose_acp_tool_call_surfaces_elicitation(isolated_home: Path) -> None:
    """A shell tool call routes through policy(ASK) -> elicitation; on approval
    the tool runs and its output reaches the conversation."""
    elicited: list[tuple[str, dict]] = []

    async def _policy(phase: str, tool: dict) -> _AskVerdict:
        assert phase == "PHASE_TOOL_CALL"
        return _AskVerdict()

    async def _elicit(tool_name: str, tool_input: dict) -> bool:
        elicited.append((tool_name, tool_input))
        return True  # user approves

    executor = GooseExecutor(
        provider=os.environ["GOOSE_PROVIDER"],
        model=os.environ["GOOSE_MODEL"],
        builtins=("developer",),
    )
    executor._policy_evaluator = _policy  # type: ignore[attr-defined]
    executor._elicitation_handler = _elicit  # type: ignore[attr-defined]

    chunks: list[str] = []
    final: TurnComplete | None = None
    try:
        async for ev in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": "Run the shell command: echo GOOSE_ACP_MARKER. Use the shell tool.",
                }
            ],
            tools=[],
            system_prompt="You are a helpful coding agent.",
        ):
            if isinstance(ev, TextChunk):
                chunks.append(ev.text)
            elif isinstance(ev, TurnComplete):
                final = ev
            elif isinstance(ev, ExecutorError):
                pytest.fail(f"executor error: {ev.message}")
    finally:
        await executor.close()

    # The shell tool call was surfaced to the elicitation handler (web card path).
    assert elicited, "expected session/request_permission -> elicitation"
    names = [name for name, _ in elicited]
    assert any("shell" in n for n in names), f"expected a shell tool, got {names}"
    # On approval the tool ran and its marker reached the streamed transcript.
    assert final is not None
    assert "GOOSE_ACP_MARKER" in ("".join(chunks) + (final.response or ""))
