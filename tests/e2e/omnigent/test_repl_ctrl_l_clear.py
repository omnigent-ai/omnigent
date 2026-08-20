"""Phase 0 characterization test — Ctrl+L clears the screen.

Submits one prompt so the scrollback has identifiable text,
presses ``Ctrl+L`` to clear the screen, then inspects the
subsequent PTY render frame. The assertion looks for (a) the
ANSI erase sequence that ``_clear_screen`` writes and (b) a
follow-up turn that completes — proving the input area is still
present and responsive. Turn synchronization uses the visible
``⠹ working`` line and the ``❯`` prompt rather than the
truncated/CPR-suppressed ``state:`` badge (see test_repl_smoke).

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Ctrl+L clear".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

# Visible turn-synchronization markers (see test_repl_smoke).
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_MODEL = "mock-model"
_HARNESS = "openai-agents"
_PROMPT = "say ok"

# The screen-clear escape sequence that prompt-toolkit's
# ``renderer.clear()`` writes when Ctrl+L fires.
_SCREEN_CLEAR_SEQ = "\x1b[2J"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0
# Time to wait for the post-Ctrl+L redraw to reach the PTY.
_POST_CLEAR_TIMEOUT = 5.0


def test_repl_ctrl_l_clears_screen(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Verify Ctrl+L writes the scrollback-clear sequence and the
    REPL keeps running afterwards.

    Uses the mock LLM server for deterministic responses.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Working directory for the
        subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "ok"},
            {"text": "hi"},
        ],
    )
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        # Send Ctrl+L; then drain the render frames.
        child.sendcontrol("l")
        post_clear_drain = drain_for(child, _POST_CLEAR_TIMEOUT)
        # Confirm the REPL is still alive and responsive.
        submit_prompt(child, "say hi")
        post_prompt_turn = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "screen_clear_sequence_written": _SCREEN_CLEAR_SEQ in post_clear_drain,
        "repl_still_alive_after_clear": "❯" in post_prompt_turn.stripped
        and "say hi" in post_prompt_turn.stripped,
    }
    diffs = compare_snapshot("test_repl_ctrl_l_clear", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+L clear:\n"
        + "\n".join(diffs)
        + f"\n\npost-clear raw (last 2000):\n"
        f"{post_clear_drain[-2000:]!r}"
    )
