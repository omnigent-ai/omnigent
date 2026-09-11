"""E2E: an oversized Codex turn input fails with a clear reason, not a raw blob.

A ``harness: codex`` turn whose user input exceeds the Codex app-server's hard
1 MiB (1048576-char) limit is rejected by the app-server with a JSON-RPC
``-32602`` error carrying ``input_error_code: input_too_large``. The executor
must surface that rejection as a clear, actionable failure reason that
preserves the observed and allowed sizes — never the raw JSON-RPC error dict
(``Codex executor error: {'code': -32602, ...}``) leaked verbatim into the
failed turn the user sees.

The journey, driven end to end through a real Codex CLI:

1. A user runs a ``harness: codex`` agent.
2. The user's turn carries an input larger than 1 MiB (a big paste, a large
   text attachment inlined as a text item, or a long serialized history).
3. The turn is sent to the Codex app-server via ``turn/start``, which rejects
   it before any model call.
4. The turn fails with a readable reason naming the input size and the limit,
   with guidance to shorten the input.

Self-contained: uses the REAL ``codex`` CLI / app-server (the component that
enforces the 1 MiB limit and produces the exact error), only never reaching an
upstream model because the app-server rejects the oversized input at
``turn/start``. Requires no server, no gateway, no credentials, and no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from tests.e2e._harness_probes import cli_unavailable_reason

# The Codex app-server's hard input ceiling and an overflow comfortably past it.
_CODEX_MAX_CHARS = 1_048_576
_OVERSIZED_CHARS = 1_450_257


@pytest.mark.posix_only
@pytest.mark.timeout(120)
async def test_codex_input_too_large_fails_turn_with_clear_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-1MiB Codex turn input fails with a readable, actionable reason.

    The real Codex app-server enforces the 1 MiB input ceiling, so this drives
    the actual production failure boundary rather than a hand-mocked error: the
    executor sends the oversized ``turn/start`` input verbatim and must
    translate the app-server's rejection for the user.
    """
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    # Isolate the app-server's data dir from any ambient ~/.codex state; no auth
    # is needed because the oversized turn is rejected before any model call.
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    executor = CodexExecutor(
        cwd=str(workspace),
        model=None,
        enable_web_search=False,
        skills_filter="none",
    )

    # A single user message whose text exceeds the app-server's 1 MiB ceiling.
    oversized_text = "A" * _OVERSIZED_CHARS

    events: list[Any] = []
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": oversized_text, "session_id": "session-1"}],
            [],
            "You are a test assistant.",
        ):
            events.append(event)
    finally:
        await executor.close()

    errors = [e for e in events if isinstance(e, ExecutorError)]
    completions = [e for e in events if isinstance(e, TurnComplete)]

    # The oversized turn must not complete -- it dies at turn/start.
    assert not completions, f"oversized turn unexpectedly completed: {events!r}"
    assert len(errors) == 1, f"expected exactly one ExecutorError, got: {events!r}"

    message = errors[0].message
    # The raw app-server JSON-RPC error dict must not leak to the user.
    for raw_fragment in ("-32602", "input_error_code", "Codex executor error:", "{'code'"):
        assert raw_fragment not in message, (
            f"raw Codex app-server error fragment {raw_fragment!r} leaked into "
            f"the failed-turn reason: {message!r}"
        )
    # The reason must preserve the observed size and the allowed limit so the
    # user can act on it.
    assert str(_OVERSIZED_CHARS) in message and str(_CODEX_MAX_CHARS) in message, (
        "expected the failed-turn reason to name the input size and Codex's "
        f"limit; got: {message!r}"
    )
    assert "Shorten" in message, (
        f"expected actionable guidance in the failed-turn reason; got: {message!r}"
    )
