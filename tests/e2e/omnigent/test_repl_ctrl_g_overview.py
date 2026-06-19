"""Phase 0 characterization test — Ctrl+G debug overview toggle.

Submits one prompt so the session has at least one message,
hits ``Ctrl+G`` to open the debug overview, asserts the
sidebar + overview pane paints (``Session: main`` header +
``debug:`` footer hints), then hits ``q`` to return to main mode
and asserts the normal status bar is back.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Ctrl+G debug overview".
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
    wait_for_ready,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-model"
_HARNESS = "openai-agents"
_PROMPT = "say ok"

# Substrings that identify overview mode.
_OVERVIEW_SESSION_HEADER = "Session: main"
_OVERVIEW_FOOTER_HINT = "debug:"

_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0
_OVERVIEW_DRAIN_TIMEOUT = 5.0


def test_repl_ctrl_g_overview_toggle(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Toggle into the debug overview with Ctrl+G and back out
    with q.

    Uses the mock LLM server for deterministic responses.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Working directory for the
        subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}])
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
        submit_prompt(child, _PROMPT)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        # Open the debug overview.
        child.sendcontrol("g")
        child.expect(_OVERVIEW_SESSION_HEADER, timeout=_OVERVIEW_DRAIN_TIMEOUT)
        overview_tail = drain_for(child, 1.0)
        overview_stripped = (
            strip_ansi(child.before or "") + _OVERVIEW_SESSION_HEADER + strip_ansi(overview_tail)
        )
        # Exit overview with 'q'.
        child.send("q")
        escape_frame_drain = drain_for(child, _OVERVIEW_DRAIN_TIMEOUT)
        escape_drain = strip_ansi(escape_frame_drain)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "overview_session_header_present": _OVERVIEW_SESSION_HEADER in overview_stripped,
        "overview_footer_hint_present": _OVERVIEW_FOOTER_HINT in overview_stripped,
        "main_mode_restored_after_esc": "state: sleeping" in escape_drain,
    }
    diffs = compare_snapshot("test_repl_ctrl_g_overview", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+G overview toggle:\n"
        + "\n".join(diffs)
        + f"\n\noverview stripped (last 2000):\n"
        f"{overview_stripped[-2000:]}"
        f"\n\nescape stripped (last 1000):\n"
        f"{escape_drain[-1000:]}"
    )
