"""Phase 0 characterization test — multi-line input via Ctrl+J.

Drives the REPL under pexpect, types the first half of a
prompt, sends ``Ctrl+J`` to insert a newline mid-input, types the
second half, and finally submits with Enter. Asserts the full
multi-line message reached the agent by looking for BOTH halves
in the rendered ``You>`` banner that the REPL echoes to
scrollback before streaming the assistant response.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Multi-line input".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    wait_for_ready,
)
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-model"
_HARNESS = "openai-agents"

# Two distinguishable halves so the assertion survives ANSI
# wrapping and prompt-toolkit's redraw minimization.
_FIRST_LINE = "line-one-alpha"
_SECOND_LINE = "line-two-beta"

# Visible turn-synchronization markers.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def test_repl_multiline_ctrl_j_insert(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Compose a two-line prompt using Ctrl+J and submit with Enter.

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
        [{"text": "I received your multi-line input."}],
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
        wait_for_ready(child, timeout=_BOOT_TIMEOUT)
        # Type the first line, insert a newline via Ctrl+J, type
        # the second line, then submit with CR.
        child.send(_FIRST_LINE)
        child.sendcontrol("j")
        child.send(_SECOND_LINE)
        child.send("\r")
        turn = await_turn_complete(
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

    # Merge with the post-exit drain.
    combined_stripped = turn.stripped + "\n" + strip_ansi(child.before or "")

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "first_line_present": _FIRST_LINE in combined_stripped,
        "second_line_present": _SECOND_LINE in combined_stripped,
        "user_banner_present": "You>" in combined_stripped,
        "agent_banner_present": "Agent>" in combined_stripped,
    }
    diffs = compare_snapshot("test_repl_multiline", observed)
    assert diffs == [], (
        "Snapshot mismatch for multi-line Ctrl+J input:\n"
        + "\n".join(diffs)
        + f"\n\nstripped buffer (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
