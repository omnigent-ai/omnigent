"""Phase 0 characterization test — sub-agent visibility in overview.

Migrated to mock LLM: the supervisor is backed by ``openai-agents``
with mock responses. Sub-agent (``claude-sdk`` and ``codex``)
parametrize rows still require the real CLI binary on PATH — those
are skipped when the binary is missing.

The core invariant remains: when a sub-agent session is registered,
the REPL overview pane must render the sub-agent's label, executor
harness, and user message.

**What breaks if this fails:**
- The ``sys_session_send`` builtin's output JSON drops
  ``conversation_id`` — the REPL's overview target registration
  keys on it.
- ``_collect_overview_targets`` stops including managed agent sessions.
- ``_render_overview_managed_session_text`` drops metadata lines.
- The wrapped harness invocation fails, so the worker never comes up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from shutil import which
from typing import Any

import pytest

from tests.e2e._harness_probes import HARNESS_HARNESS_MODELS, HARNESS_IDS
from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
    wait_for_ready,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

# Supervisor model — mock LLM serves deterministic responses.
_SUPERVISOR_MODEL = "mock-overview-subagent-supervisor"
_SUPERVISOR_HARNESS = "openai-agents"

# Mapping from harness id to the YAML's worker tool name.
_WORKER_TOOL_BY_HARNESS: dict[str, str] = {
    "claude-sdk": "claude_worker",
    "codex": "codex_worker",
}

# Per-harness substring set the rendered Executor: line might print.
_EXECUTOR_MARKERS_BY_HARNESS: dict[str, tuple[str, ...]] = {
    "claude-sdk": ("claude-sdk", "claudesdk"),
    "codex": ("codex",),
}

_SUBAGENT_MESSAGE_CONTENT = "say hello"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 60.0
_RUNNING_TIMEOUT = 30.0
_COMPLETION_TIMEOUT = 240.0
_EXIT_TIMEOUT = 15.0
_OVERVIEW_DRAIN_TIMEOUT = 6.0
_EXPECT_SUBAGENT_TIMEOUT = 30.0


def _check_worker_harness_available(harness: str, omnigent_python: Path) -> None:
    """
    Fail loud if the worker harness's prerequisites are missing.

    :param harness: The worker harness identifier under test.
    :param omnigent_python: The subprocess interpreter.
    """
    if harness == "claude-sdk":
        probe = subprocess.run(
            [
                str(omnigent_python),
                "-c",
                "import importlib.util, sys; "
                "sys.exit(0 if importlib.util.find_spec('claude_agent_sdk') else 1)",
            ],
            capture_output=True,
        )
        if probe.returncode != 0 or which("claude") is None:
            pytest.fail(
                "claude-sdk prerequisites missing: need both the "
                "'claude_agent_sdk' Python package and the 'claude' "
                "CLI binary on PATH."
            )
    elif harness == "codex":
        if which("codex") is None:
            pytest.fail(
                "codex prerequisite missing: the 'codex' CLI binary "
                "must be installed on PATH (install via "
                "'npm i -g @openai/codex')."
            )


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_repl_overview_subagent_visibility(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    harness: str,
    model: str,
) -> None:
    """
    Spawn a supervisor that delegates to a sub-agent worker, open
    the overview, cycle to the sub-agent target, and verify
    its metadata lines render.

    Uses the mock LLM server for supervisor responses. Sub-agent
    harnesses (claude-sdk, codex) still require their respective CLI
    binaries on PATH — rows are skipped when those are absent.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL.
    :param harness: Worker harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: Model identifier (unused at CLI level; accepted
        to match the parametrize shape).
    """
    if harness not in _WORKER_TOOL_BY_HARNESS:
        # ``coding_supervisor.yaml`` only defines worker tools for
        # claude-sdk and codex. Other harnesses skip cleanly.
        pytest.skip(
            f"{harness!r} has no <harness>_worker tool in "
            f"tests/resources/examples/coding_supervisor.yaml; this test requires the "
            f"YAML to declare an AgentTool for the harness."
        )
    _check_worker_harness_available(harness, omnigent_python)
    worker_tool = _WORKER_TOOL_BY_HARNESS[harness]
    worker_label_prefix = f"{worker_tool}:"
    executor_markers = _EXECUTOR_MARKERS_BY_HARNESS[harness]
    user_prompt = (
        f"Delegate to {worker_tool}. Call sys_session_send with "
        f"tool={worker_tool}, session=demo, and input='say hello'. "
        f"Do not answer inline. After the worker replies, relay its "
        f"message verbatim."
    )
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "coding_supervisor.yaml"

    # Configure mock supervisor: tool call to sys_session_send, then text.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_session_send",
                        "name": "sys_session_send",
                        "arguments": (
                            f'{{"tool": "{worker_tool}", "session": "demo", "input": "say hello"}}'
                        ),
                    }
                ]
            },
            {"text": "The worker said: hello"},
        ],
        key=_SUPERVISOR_MODEL,
    )

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_SUPERVISOR_MODEL,
        harness=_SUPERVISOR_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        wait_for_ready(child, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, user_prompt)
        # Wait for the tool-call line that represents the
        # ``sys_session_send`` invocation for the worker.
        child.expect(
            rf"sys_session_send \({worker_tool}:",
            timeout=_COMPLETION_TIMEOUT,
        )
        # Open the overview.
        child.sendcontrol("g")
        # Drain follow-up frames so the buffer has the sidebar + main
        # session pane.
        drain_for(child, _OVERVIEW_DRAIN_TIMEOUT)
        # Tab cycles to the next target.
        child.send("\t")
        # Wait for the sub-agent's label to appear in the pane header.
        child.expect(
            f"Session: {worker_label_prefix}",
            timeout=_EXPECT_SUBAGENT_TIMEOUT,
        )
        subagent_pane_tail = drain_for(child, _OVERVIEW_DRAIN_TIMEOUT)
        subagent_stripped = (
            strip_ansi(child.before or "")
            + f"Session: {worker_label_prefix}"
            + strip_ansi(subagent_pane_tail)
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "subagent_label_present": worker_label_prefix in subagent_stripped,
        "subagent_executor_harness_rendered": any(
            marker in subagent_stripped for marker in executor_markers
        ),
        "subagent_user_message_rendered": _SUBAGENT_MESSAGE_CONTENT in subagent_stripped,
    }
    diffs = compare_snapshot("test_repl_overview_subagent_visibility", observed)
    assert diffs == [], (
        "Snapshot mismatch for sub-agent overview visibility:\n"
        + "\n".join(diffs)
        + f"\n\nsubagent stripped (last 2500):\n"
        f"{subagent_stripped[-2500:]}"
    )
