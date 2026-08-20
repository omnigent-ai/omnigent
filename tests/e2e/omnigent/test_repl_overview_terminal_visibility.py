"""Phase 0 characterization test — terminal visibility in overview.

Migrated to mock LLM: uses canned tool-call responses to trigger
``sys_terminal_launch`` without real Databricks credentials.

Uses ``examples/terminal_workers.yaml`` so the REPL hosts a
terminal-supervisor. Sends a prompt that (via the mock response)
invokes ``sys_terminal_launch``, waits for the turn to complete, opens
the overview, cycles to the terminal target, and asserts the overview
pane renders the tmux ``attach`` instruction line.

The supervisor runs the ``openai-agents`` harness (not the YAML's
default ``open-responses``): under the mock LLM server, ``open-responses``
fails to spawn on the runner (``harness_spawn_failed`` / ``runner_error``)
so the terminal never launches — a mock-incompatibility analogous to the
documented ``claude-sdk`` case, not a stale marker. ``openai-agents`` is
mock-compatible and exercises the same terminal-launch + overview path.

**What breaks if this fails:**
- ``_collect_overview_targets`` stops including terminal instances.
- ``_render_overview_terminal_text`` drops the attach instruction.
- ``_terminal_attach_command`` stops composing the tmux command.
- ``sys_terminal_launch`` fails to register the instance.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from shutil import which
from typing import Any

import pexpect
import pytest

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
    wait_for_ready,
)
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-overview-terminal"
_HARNESS = "openai-agents"

_PROMPT = (
    'Call sys_terminal_launch(terminal="shell", session="probe") '
    "and then tell me you're done. Do not call any other tools."
)

_TERMINAL_LABEL = "shell:probe"
_ATTACH_TMUX_MARKER = "tmux -S"
_ATTACH_VERB_MARKER = "attach"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 60.0
_RUNNING_TIMEOUT = 30.0
_COMPLETION_TIMEOUT = 120.0
_EXIT_TIMEOUT = 15.0
_OVERVIEW_DRAIN_TIMEOUT = 6.0
_EXPECT_TERMINAL_TIMEOUT = 15.0


@pytest.fixture
def tmux_available() -> bool:
    """
    Skip-guard: terminal tools require ``tmux`` on PATH.

    :returns: True when ``tmux`` is available.
    """
    return which("tmux") is not None


def test_repl_overview_terminal_visibility(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmux_available: bool,
) -> None:
    """
    Launch a terminal session through the supervisor (mock LLM), open
    the overview, cycle to the terminal target, and verify the tmux
    attach instructions render.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL.
    :param tmux_available: True when ``tmux`` is on PATH.
        Terminal tools require tmux; fail loud when missing.
    """
    if not tmux_available:
        pytest.fail(
            "tmux binary not found on PATH — terminal-tool tests "
            "require tmux to be installed (``brew install tmux``)."
        )

    # Mock: first response is a sys_terminal_launch tool call, second
    # is the text completion after the launch completes.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_terminal_launch",
                        "name": "sys_terminal_launch",
                        "arguments": '{"terminal": "shell", "session": "probe"}',
                    }
                ]
            },
            {"text": "I'm done launching the terminal."},
        ],
        key=_MODEL,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "terminal_workers.yaml"

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
        # Sync on the supervisor's FINAL reply text, which renders only after
        # sys_terminal_launch executed and registered the terminal as an
        # overview target. (The old "• sys_terminal_launch (Nms)" completion
        # line is retired — the tool-call now renders as
        # "⏵ sys_terminal_launch({...})" — and that line carries ANSI between
        # the name and "(", so syncing on it is unreliable.)
        child.expect("launching the terminal", timeout=_COMPLETION_TIMEOUT)
        # Open the overview (Ctrl+O; binding moved off Ctrl+G — Warp intercepts
        # Ctrl+G, see _repl.py "Why Ctrl+O and not Ctrl+G").
        child.sendcontrol("o")
        # Wait for the sidebar to paint the terminal target ("💻 shell:probe").
        child.expect(_TERMINAL_LABEL, timeout=_EXPECT_TERMINAL_TIMEOUT)
        # Tab cycles main → shell:probe so the terminal detail pane (the tmux
        # attach instructions, "tmux -S <sock> attach …") renders.
        child.send("\t")
        # Accumulate the detail pane. The status-bar clock ticks ~1×/s, so a
        # drain that bails on a 0.3s idle gap is unreliable — force a
        # fixed-duration read with an impossible-pattern expect.
        with contextlib.suppress(pexpect.TIMEOUT):
            child.expect("ZZZ_NEVER_MATCHES_DRAIN", timeout=_OVERVIEW_DRAIN_TIMEOUT)
        terminal_stripped = _TERMINAL_LABEL + strip_ansi(child.before or "")
        # Close the overlay before teardown ('q'); leaving it open blocks the
        # Ctrl+D / "/quit" exit handshake (see test_repl_ctrl_o_overview).
        child.send("q")
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "terminal_label_present": _TERMINAL_LABEL in terminal_stripped,
        "tmux_socket_flag_rendered": _ATTACH_TMUX_MARKER in terminal_stripped,
        "attach_verb_rendered": _ATTACH_VERB_MARKER in terminal_stripped,
    }
    diffs = compare_snapshot("test_repl_overview_terminal_visibility", observed)
    assert diffs == [], (
        "Snapshot mismatch for terminal overview visibility:\n"
        + "\n".join(diffs)
        + f"\n\nterminal stripped (last 2500):\n"
        f"{terminal_stripped[-2500:]}"
    )
