"""Phase 0 characterization test — Up-arrow history recall.

Submits two distinguishable prompts, then presses the Up arrow
and asserts the previous prompt's text repopulates the input
area. The assertion looks for the recalled prompt in the
rendered PTY buffer *after* Up is pressed — prompt-toolkit
paints the input window with the recalled entry on the next
render tick.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "History recall".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
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

# Two distinct prompts whose text can be unambiguously recognized
# in the rendered PTY buffer.
_PROMPT_ONE = "hello-marker-alpha"
_PROMPT_TWO = "hello-marker-beta"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0

# How long to wait for the Up-arrow redraw.
_RECALL_TIMEOUT = 3.0


def test_repl_history_recall_up_arrow(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Submit two prompts, press Up, and verify the most-recent one
    repopulates the input area.

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
            {"text": "Response to alpha prompt."},
            {"text": "Response to beta prompt."},
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
        submit_prompt(child, _PROMPT_ONE)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        submit_prompt(child, _PROMPT_TWO)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        # Press Up to recall _PROMPT_TWO into the input area.
        child.send("\x1b[A")  # ANSI escape for Up arrow
        recall_drain = drain_for(child, _RECALL_TIMEOUT)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    combined_stripped = strip_ansi(recall_drain) + "\n" + strip_ansi(child.before or "")

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "recalled_prompt_in_input_area": _PROMPT_TWO in combined_stripped,
    }
    diffs = compare_snapshot("test_repl_history_recall", observed)
    assert diffs == [], (
        "Snapshot mismatch for history recall (Up arrow):\n"
        + "\n".join(diffs)
        + f"\n\nrecall-drain stripped (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
