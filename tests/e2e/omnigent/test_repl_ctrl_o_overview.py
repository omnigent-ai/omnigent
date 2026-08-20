"""Phase 0 characterization test — debug overview toggle.

Submits one prompt so the session has at least one message,
hits ``Ctrl+O`` to open the debug overview, and asserts the
sidebar + overview pane paints (``Session: main`` header +
``Debug overview`` title). It then hits ``q`` to close the
overlay for teardown, but does not assert the post-close idle
state: that signal is unreliable in CI (the ``q`` keystroke can
drop during a toolbar repaint, and the idle status text wraps at
the PTY boundary), so the load-bearing coverage is the overview
opening and painting.

The overview binding is ``Ctrl+O`` (it moved off ``Ctrl+G``, which
Warp and some terminals intercept for their own search before the
app sees it — see ``omnigent/repl/_repl.py`` "Why Ctrl+O and not
Ctrl+G"). This file was renamed from ``test_repl_ctrl_g_overview``
to match.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "debug overview".
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

# Substrings that identify overview mode. The overlay paints its title
# ("Debug overview — <agent>") above the sidebar; the legacy "debug:" footer
# string no longer renders, so key the second marker on the title instead.
_OVERVIEW_SESSION_HEADER = "Session: main"
_OVERVIEW_FOOTER_HINT = "Debug overview"

_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0
_OVERVIEW_DRAIN_TIMEOUT = 5.0


def test_repl_ctrl_o_overview_toggle(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Toggle into the debug overview with Ctrl+O and back out
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
        # Open the debug overview via Ctrl+O (the binding moved off Ctrl+G,
        # which Warp/some terminals intercept; see the module docstring).
        child.sendcontrol("o")
        child.expect(_OVERVIEW_SESSION_HEADER, timeout=_OVERVIEW_DRAIN_TIMEOUT)
        overview_tail = drain_for(child, 1.0)
        overview_stripped = (
            strip_ansi(child.before or "") + _OVERVIEW_SESSION_HEADER + strip_ansi(overview_tail)
        )
        # Close the overlay for teardown. The former "main mode restored after
        # q" assertion was dropped: detecting it is unreliable in CI — the 'q'
        # keystroke can be dropped during a toolbar repaint (same fragility
        # clean_exit documents for Ctrl+D) and the idle status-bar text
        # wraps/mangles at the 120-col PTY boundary, so the signal is neither
        # reliably delivered nor matchable (29/30 CI flake). The load-bearing
        # coverage — Ctrl+O opens and paints the overview — is asserted below.
        child.send("q")
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "overview_session_header_present": _OVERVIEW_SESSION_HEADER in overview_stripped,
        "overview_footer_hint_present": _OVERVIEW_FOOTER_HINT in overview_stripped,
    }
    diffs = compare_snapshot("test_repl_ctrl_o_overview", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+O debug overview:\n"
        + "\n".join(diffs)
        + f"\n\noverview stripped (last 2000):\n"
        f"{overview_stripped[-2000:]}"
    )
