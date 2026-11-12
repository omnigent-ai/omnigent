"""Phase 0 characterization test — Ctrl+L clears the screen.

Submits one prompt so the scrollback has identifiable text,
presses ``Ctrl+L`` to clear the screen, then inspects the
subsequent PTY render frame. The assertion looks for (a) the
ANSI erase sequence that ``_clear_screen`` writes and (b) a
follow-up turn that completes — proving the input area is still
present and responsive. Distinct assistant replies identify
the turns, since the input prompt also appears during streaming.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Ctrl+L clear".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_PROMPT_MARKER = r"❯ "

_MODEL = "mock-model"
_HARNESS = "openai-agents"
_PROMPT = "say ok"
_FIRST_REPLY = "First clear check response"
_SECOND_REPLY = "Second clear check response"

# The screen-clear escape sequence that prompt-toolkit's
# ``renderer.clear()`` writes when Ctrl+L fires.
_SCREEN_CLEAR_SEQ = "\x1b[2J"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0
# Time to wait for the post-Ctrl+L redraw to reach the PTY.
_POST_CLEAR_TIMEOUT = 5.0


@pytest.mark.parametrize("response_delay", [0.0, 1.0])
def test_repl_ctrl_l_clears_screen(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    response_delay: float,
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
    :param response_delay: Mock response delay covering immediate and pending turns.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": _FIRST_REPLY, "delay": response_delay},
            {"text": _SECOND_REPLY, "delay": response_delay},
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
        child.expect(_PROMPT_MARKER, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        child.expect_exact(_FIRST_REPLY, timeout=_COMPLETION_TIMEOUT)
        child.sendcontrol("l")
        child.expect_exact(_SCREEN_CLEAR_SEQ, timeout=_POST_CLEAR_TIMEOUT)
        post_clear_output = (child.before or "") + child.after
        submit_prompt(child, "say hi")
        child.expect_exact(_SECOND_REPLY, timeout=_COMPLETION_TIMEOUT)
        post_prompt_output = strip_ansi((child.before or "") + child.after)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "screen_clear_sequence_written": _SCREEN_CLEAR_SEQ in post_clear_output,
        "repl_still_alive_after_clear": "say hi" in post_prompt_output
        and _SECOND_REPLY in post_prompt_output,
    }
    diffs = compare_snapshot("test_repl_ctrl_l_clear", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+L clear:\n"
        + "\n".join(diffs)
        + f"\n\npost-clear raw (last 2000):\n"
        f"{post_clear_output[-2000:]!r}"
        f"\n\nfollow-up output (last 2000):\n{post_prompt_output[-2000:]!r}"
    )
