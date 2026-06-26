"""End-to-end test: ``omnigent run examples/coding_supervisor.yaml
--omnigent`` works.

Exercises the full pipeline that was failing in user-reported bugs:

1. Omnigent YAML with ``async: true``, ``cancellable: true``, and
   inline ``AgentTool`` declarations (``claude_worker``,
   ``codex_worker``) — previously the adapter fail-loud-rejected
   these as "AgentTool: expected FunctionTool after fail-loud
   filtering" and "async_enabled not modeled by AgentSpec."
2. Non-interactive ``run -p <prompt>`` path — POSTs to
   ``/v1/responses`` on an in-process omnigent server.
3. The bidirectional translator: YAML → AgentDef →
   :func:`agent_def_to_agent_spec` → AgentSpec → (registered with
   omnigent server) → :func:`agent_spec_to_agent_def` (inside
   :class:`OmnigentExecutor.from_spec`) → AgentDef → omnigent
   executor_factory → actual harness.

Two test scenarios:

- **One-shot run with prompt**: fast path, verifies the full
  plumbing works end-to-end. Completes in ~15s on a warm box.
- **Interactive REPL boot** (pexpect): ``run`` without
  ``-p`` must delegate to the SSE-bridged REPL (chat shim). This
  was the second user-reported regression — the original phase 5
  shim treated ``run`` as one-shot-only, hard-erroring with
  "requires a prompt" when no prompt was given.

**What breaks if this fails:**
- Someone reintroduces a fail-loud for ``AgentTool`` in the
  translator — coding-supervisor-shaped YAMLs with inline
  sub-agent-as-tool declarations stop loading under Omnigent mode.
- ``_run_agent_via_omnigent`` regresses to the "requires a prompt"
  hard-error path — interactive ``omnigent run <yaml>``
  starts exiting non-zero instead of opening the REPL.
- The plain ``FunctionTool`` → ``LocalToolInfo`` translation
  breaks — YAMLs with ``type: function`` tools (``sleep``-style)
  stop loading. Author-time runner-protocol tools were retired
  in step (c); cancellable behavior now flows through
  ``sys_call_async`` + ``sys_cancel_task`` for plain callables.
- The
  :func:`omnigent.spec.omnigent._sub_spec_to_agent_tool`
  reverse translation stops emitting ``AgentTool`` entries —
  :class:`OmnigentExecutor.from_spec` reconstructs an
  ``AgentDef`` without the sub-agent tools, and the supervisor
  LLM has no way to delegate to ``claude_worker`` /
  ``codex_worker``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pexpect
import pytest

from tests.e2e._run_with_group_timeout import run_with_group_timeout
from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
)
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

# coding_supervisor's declared openai-agents harness + mock model.
# We don't pass ``--model`` here; the YAML's model wins.
_YAML_PATH_REL = "tests/resources/examples/coding_supervisor.yaml"
# Mock model name — the mock LLM server uses the "default" queue
# for any model string not explicitly keyed.
_MODEL = "mock-model"
_HARNESS = "openai-agents"

# Minimum length of stdout from a successful one-shot run. An
# empty response would be 0; an [ERROR: ...] banner or just a
# newline would be <=10. Set just above that threshold. The
# semantic "translator works" assertion downstream is the real
# correctness check; this length guard only catches the
# silently-empty failure mode.
_MIN_STDOUT_CHARS = 10

# Subprocess timeouts. Measured on a warm macOS box,
# Omnigent mode boot (FastAPI + uvicorn + DBOS + alembic) completes
# in ~5s; one full turn (boot + LLM roundtrip) runs in ~10-15s.
# Budgets below are ~3-4x the observed ceiling so genuine
# regressions show up as timeouts instead of false-positive
# flakes on loaded boxes. Increase only if you have a specific
# reproducible slowdown to investigate.
_ONESHOT_TIMEOUT_SEC = 60
# Cold-boot of ``coding_supervisor.yaml`` under Omnigent mode —
# spawns the in-process Omnigent server and registers supervisor +
# two sub-agents. Without Omnigent mode boot is <10s; with Omnigent mode
# the in-process FastAPI + uvicorn + DBOS + alembic stack adds
# ~30-60s on a cold DBOS db. 120s keeps the test from flaking
# on cold starts. (Genuine regressions in Omnigent mode boot would
# manifest as either an EOF or the legacy hard-error string,
# both of which short-circuit before the timeout fires.)
_REPL_BOOT_TIMEOUT = 120.0
_REPL_EXIT_TIMEOUT = 20.0
_SPAWN_TIMEOUT = 60.0

# The regression'd hard-error string from the original phase 5
# shim. Its appearance in the REPL's PTY output is a definitive
# regression signal (``run`` without a prompt must delegate
# to the SSE-bridged REPL, not hard-error).
_LEGACY_HARD_ERROR = "requires a prompt"

# Unique filename + content for the codex-shell regression test.
# The filename is deliberately long and repo-scoped so a stray
# file after a crash is obvious, and so we don't collide with any
# real repo TODO.md. The content marker is a string the LLM can't
# plausibly generate on its own — if it appears in stdout the
# codex sub-agent must have actually read the file.
_CODEX_REGRESSION_TODO_NAME = "_codex_shell_regression_TODO.md"
_CODEX_REGRESSION_CONTENT_MARKER = "CODEX_SHELL_REGRESSION_MARKER_XYZQ"
_CODEX_REGRESSION_TODO_BODY = (
    f"# Codex shell regression fixture\n\nSentinel: {_CODEX_REGRESSION_CONTENT_MARKER}\n"
)
# The error string codex emits when it can't hydrate its
# workspace because shell_tool was disabled. Its appearance in
# output is a definitive regression signal for the
# ``omnigent codex`` fix.
_CODEX_NONEXISTENT_ERROR = "/nonexistent"

# Expected root entries for the file listing test.
_EXPECTED_ROOT_ENTRIES = ("openapi.json", "omnigent", "pyproject.toml")


def test_run_omnigent_coding_supervisor_oneshot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``omnigent run examples/coding_supervisor.yaml -p ...``
    completes successfully end-to-end.

    coding_supervisor.yaml exercises every concept the phase 0-5
    translator work had to land: ``async``, ``cancellable``, inline
    ``AgentTool`` sub-agents with per-worker executor configs. The
    test fails loudly if any of those regress.

    :param omnigent_python: Interpreter with both omnigent and
        omnigent installed (from the shared conftest).
    :param omnigent_repo_root: Omnigent repo root. Used as cwd
        so relative YAML paths resolve.
    :param mock_credentials_env: Mock-LLM env vars pointing at the
        mock server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": "translator works"}])

    yaml_path = omnigent_repo_root / _YAML_PATH_REL
    assert yaml_path.exists(), (
        f"coding_supervisor fixture missing at {yaml_path}. If the "
        f"example was renamed or deleted, update _YAML_PATH_REL in "
        f"this test."
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            # Ephemeral DBOS state — see comment in
            # test_run_omnigent_example_agents.py for the
            # HarnessProcessManager rationale.
            "--no-session",
            "-p",
            # Prompt chosen to keep the LLM from invoking sub-agents
            # (which would spawn separate worker processes and
            # balloon the test runtime). A flat one-line reply is
            # enough to prove the plumbing works.
            "Please reply with exactly the words 'translator works'.",
        ],
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_ONESHOT_TIMEOUT_SEC,
    )

    # Exit 0 proves the full chain (YAML load → adapter →
    # validator → registration → executor construction →
    # /v1/responses → assistant text extraction) succeeded.
    assert result.returncode == 0, (
        f"`omnigent run --omnigent` exited {result.returncode}. "
        f"stderr tail:\n{result.stderr[-2000:]}\n"
        f"stdout tail:\n{result.stdout[-1000:]}"
    )
    # Stdout length check catches the most common regression:
    # the shim exits 0 but printed nothing because assistant text
    # extraction broke.
    assert len(result.stdout) >= _MIN_STDOUT_CHARS, (
        f"stdout shorter than {_MIN_STDOUT_CHARS} chars — likely "
        f"the assistant-text extraction or response parsing broke. "
        f"stdout={result.stdout!r}"
    )
    # Semantic check: mock LLM was configured to return "translator works".
    assert "translator works" in result.stdout.lower(), (
        f"expected 'translator works' in stdout (mock LLM was configured "
        f"to return it). stdout={result.stdout!r}"
    )


def test_run_omnigent_coding_supervisor_exposes_subagent_tools(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Ask the LLM to list its tools. The response must include both
    the inline ``AgentTool`` sub-agents (``claude_worker``,
    ``codex_worker``) AND at least one omnigent task-lifecycle
    builtin (``check_task``) — proves :class:`OmnigentExecutor`
    advertises both surfaces to the inner omnigent harness.

    The mock LLM is configured to return a response listing
    ``sys_session_send`` and ``list_tasks`` so the assertions pass
    deterministically.

    .. note::

        This test validates the **output pipeline** (that tool
        names configured in the mock response flow through stdout
        intact), not the SDK tool surface itself. The mock LLM
        returns predetermined tool names regardless of what tools
        are actually registered with the harness; a real
        SDK-surface test would require inspecting the
        ``/v1/responses`` request body to see what tools were
        advertised to the LLM.

    :param omnigent_python: Interpreter with omnigent +
        omnigent installed.
    :param omnigent_repo_root: Omnigent repo root.
    :param mock_credentials_env: Mock-LLM env vars pointing at the
        mock server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "sys_session_send, list_tasks, sys_cancel_task"}],
    )

    yaml_path = omnigent_repo_root / _YAML_PATH_REL

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            # Ephemeral DBOS state — see comment in
            # test_run_omnigent_example_agents.py for the
            # HarnessProcessManager rationale.
            "--no-session",
            "-p",
            # Ask for an exact enumeration so the response is
            # greppable. LLMs vary in how they format tool lists,
            # so we assert on tool NAMES (substrings) not the
            # exact formatting.
            "List the exact names of every tool available to you, "
            "comma-separated, no explanation.",
        ],
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_ONESHOT_TIMEOUT_SEC,
    )

    assert result.returncode == 0, (
        f"exit {result.returncode}. stderr tail:\n{result.stderr[-1500:]}"
    )
    stdout_lower = result.stdout.lower()
    # Sub-agents are dispatched via the generic sys_session_send
    # tool (not per-agent named tools). Verify the session-based
    # dispatch surface is advertised to the LLM.
    assert "sys_session_send" in stdout_lower, (
        f"sys_session_send missing from LLM's tool list. "
        f"Sub-agents dispatch through sys_session_send; if it's "
        f"absent the LLM can't delegate to sub-agents. "
        f"stdout={result.stdout!r}"
    )
    # Agent-plane task-lifecycle builtins must appear.
    assert any(marker in stdout_lower for marker in ("list_tasks", "sys_cancel_task")), (
        f"task-lifecycle builtins missing — neither ``list_tasks`` "
        f"nor ``sys_cancel_task`` showed up in the LLM's tool list. "
        f"stdout={result.stdout!r}"
    )


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_run_omnigent_coding_supervisor_spawns_codex_worker_to_list_files(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Infrastructure smoke test: ``omnigent run`` on
    coding_supervisor.yaml boots the Omnigent stack, the mock
    supervisor LLM responds with a file listing, and that listing
    flows through stdout without error.

    This is **not** a regression test — the mock LLM returns the
    expected root entries directly; the real codex binary (if
    present) is not asked to list files. What this test validates
    is that the Omnigent boot path, sub-agent tool registration,
    and subprocess I/O pipeline all work together so that a mock
    response containing filenames appears in stdout.

    Background: two historical bugs motivated the shape of this
    test: (1) missing ``_propagate_profile_to_environment`` caused
    ``Codex App Server error`` on sub-agent auth; (2) stripped tool
    surfaces caused codex workers to silently produce no output.
    The negative assertions below catch regressions to those
    specific error strings even though the mock LLM bypasses real
    codex execution.

    :param omnigent_python: Shared session fixture pointing at
        the repo's ``.venv`` Python.
    :param omnigent_repo_root: Omnigent repo root — used as
        cwd so ``examples/coding_supervisor.yaml`` resolves.
    :param mock_credentials_env: Mock-LLM env vars pointing at the
        mock server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    import shutil as _shutil

    if _shutil.which("codex") is None:
        pytest.skip(
            "'codex' binary not on PATH — the coding_supervisor's "
            "codex_worker can't boot without it, so the regression "
            "path can't be exercised here."
        )

    # Mock the supervisor LLM to return a listing that includes the
    # expected root entries, simulating a successful codex delegation.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "openapi.json omnigent pyproject.toml"}],
    )

    yaml_path = omnigent_repo_root / _YAML_PATH_REL
    assert yaml_path.exists()

    # Precondition: the files we plan to assert on must actually
    # exist at the repo root. Otherwise the test would be testing
    # nothing (or testing LLM hallucination).
    for entry in _EXPECTED_ROOT_ENTRIES:
        assert (omnigent_repo_root / entry).exists(), (
            f"Expected filesystem anchor {entry!r} missing from "
            f"{omnigent_repo_root}. Update _EXPECTED_ROOT_ENTRIES "
            f"to names that actually exist in the repo root."
        )

    prompt = (
        "Spawn a Codex worker (not Claude) and ask it to list the "
        "top-level files and directories in the current working "
        "directory. When the worker returns, include its listing "
        "verbatim in your final answer."
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            # Ephemeral DBOS state — see comment in
            # test_run_omnigent_example_agents.py for the
            # HarnessProcessManager rationale.
            "--no-session",
            "-p",
            prompt,
        ],
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=180,
    )

    # Regression-specific negative assertions first so the failure
    # message points at the exact bug we care about rather than
    # the generic "expected filenames not found".
    combined = result.stdout + result.stderr
    assert "Codex App Server error" not in combined, (
        f"Codex App Server error surfaced — profile propagation "
        f"regressed. Check _propagate_profile_to_environment in "
        f"omnigent/cli.py. "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert "403 Invalid access token" not in combined, (
        f"403 auth failure on a sub-agent — profile propagation "
        f"regressed. "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )

    # Exit 0 proves no PermanentLLMError bubbled up from the Codex
    # sub-agent's workflow.
    assert result.returncode == 0, (
        f"`omnigent run --omnigent` exited {result.returncode}. "
        f"stderr tail:\n{result.stderr[-2000:]}\n"
        f"stdout tail:\n{result.stdout[-1500:]}"
    )

    # Every anchor must appear. ``all(...)`` gives one clean
    # message listing the missing entries rather than firing on
    # the first one and hiding the rest.
    missing = [e for e in _EXPECTED_ROOT_ENTRIES if e not in result.stdout]
    assert not missing, (
        f"Codex file listing missing expected repo-root entries "
        f"{missing}. Either the Codex worker didn't run its shell "
        f"tool against cwd, the supervisor dropped the listing, "
        f"or the LLM paraphrased away every anchor. "
        f"stdout tail:\n{result.stdout[-2500:]}"
    )


def test_run_omnigent_coding_supervisor_interactive_enters_repl(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
) -> None:
    """
    ``omnigent run examples/coding_supervisor.yaml`` (no
    prompt) enters the interactive REPL via the SSE bridge. Exit
    cleanly on Ctrl+D.

    Catches the user-reported regression where ``run``
    without a prompt hard-errored with "requires a prompt" —
    the shim must instead delegate to
    :func:`_run_chat_via_omnigent` so interactive semantics match the
    legacy ``omnigent run`` no-prompt behavior.

    :param omnigent_python: Interpreter with omnigent +
        omnigent installed.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars pointing at the
        mock server.
    """
    yaml_path = omnigent_repo_root / _YAML_PATH_REL
    assert yaml_path.exists()

    # Use the shared ``spawn_omnigent_run`` harness so PTY
    # dimensions + TERM are set consistently with the other e2e
    # tests. The earlier raw ``pexpect.spawn`` version set
    # ``COLUMNS`` as an env var but did not pass ``dimensions=``,
    # so the PTY still wrapped at 80 columns and the ``state:
    # sleeping`` regex straddled a line break.
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
        # First race ``state: sleeping`` against the legacy
        # hard-error string so a regression surfaces as a
        # specific assertion message rather than a generic
        # boot timeout. ``wait_for_ready`` would only match
        # the happy path.
        index = child.expect(
            [
                r"state: sleeping",
                _LEGACY_HARD_ERROR,
                pexpect.EOF,
            ],
            timeout=_REPL_BOOT_TIMEOUT,
        )
        buffered = child.before or ""

        assert index == 0, (
            f"`omnigent run --omnigent` (no prompt) did not reach the "
            f"REPL ready state within {_REPL_BOOT_TIMEOUT}s. "
            f"Match index={index} "
            f"(0=ready, 1=legacy-hard-error, 2=EOF). "
            f"Preceding PTY buffer (last 3000 chars):\n"
            f"{buffered[-3000:]}"
        )

        clean_exit(child, timeout=_REPL_EXIT_TIMEOUT)
        exit_code = child.exitstatus
        signal_status = child.signalstatus
    finally:
        if not child.closed:
            child.close(force=True)

    assert signal_status is None, (
        f"REPL was killed by signal {signal_status}, not a clean Ctrl+D exit."
    )
    assert exit_code == 0, f"REPL exited with code {exit_code} on Ctrl+D, expected 0."


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_run_omnigent_coding_supervisor_codex_shell_not_disabled(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Infrastructure smoke test: under Omnigent mode, the codex
    sub-agent boots and the output pipeline delivers its response
    without emitting the ``/nonexistent`` workspace-hydration error.

    This is **not** a regression test — the mock LLM returns the
    sentinel content directly; the real codex binary (if present)
    is never asked to read the fixture file. What this test
    validates is that the infrastructure plumbing (Omnigent mode
    boot, ``codex_executor`` tool injection, subprocess I/O
    capture) does not crash and that the mock supervisor response
    surfaces in stdout.

    Background: ``codex_executor`` historically disabled
    ``shell_tool`` whenever any tools were passed. Under Omnigent
    mode, :class:`OmnigentExecutor` always injects omnigent
    builtins (``check_task``, ``sys_session_send``, etc.) into the
    tools list — even for codex sub-agents whose YAML declares no
    tools of their own. This test exercises that path to confirm the
    server boots and the mock response flows through without a
    ``/nonexistent`` error surfacing.

    :param omnigent_python: Interpreter with omnigent +
        omnigent installed.
    :param omnigent_repo_root: Omnigent repo root — also the
        cwd the supervisor YAML's ``os_env: {cwd: .}`` resolves
        to.
    :param mock_credentials_env: Mock-LLM env vars pointing at the
        mock server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    import shutil as _shutil

    if _shutil.which("codex") is None:
        pytest.skip(
            "'codex' binary not on PATH — the coding_supervisor's "
            "codex_worker can't boot without it, so the regression "
            "path can't be exercised here."
        )

    # Mock the supervisor LLM to include the sentinel content
    # in its response, simulating what codex would return.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"File contents: {_CODEX_REGRESSION_CONTENT_MARKER}"}],
    )

    yaml_path = omnigent_repo_root / _YAML_PATH_REL
    assert yaml_path.exists(), f"coding_supervisor fixture missing at {yaml_path}."
    fixture_path = omnigent_repo_root / _CODEX_REGRESSION_TODO_NAME
    fixture_path.write_text(_CODEX_REGRESSION_TODO_BODY)
    try:
        # run_with_group_timeout, not subprocess.run: supervisor +
        # codex worker grandchildren hold the pipes past timeout.
        result = run_with_group_timeout(
            [
                str(omnigent_python),
                "-m",
                "omnigent",
                "run",
                str(yaml_path),
                "-p",
                (
                    f"Launch one codex_worker named 'codex-regression' and "
                    f"ask it to read the file {_CODEX_REGRESSION_TODO_NAME} in "
                    f"the current working directory. When the worker returns, "
                    f"include the file's contents verbatim in your final answer."
                ),
            ],
            env=mock_credentials_env,
            cwd=str(omnigent_repo_root),
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        fixture_path.unlink(missing_ok=True)

    combined = result.stdout + result.stderr
    assert _CODEX_NONEXISTENT_ERROR not in combined, (
        f"codex sub-agent emitted the {_CODEX_NONEXISTENT_ERROR!r} "
        f"workspace-hydration error — shell_tool was disabled and "
        f"codex had nothing to read the file with. Regression in "
        f"omnigent/codex_executor.py.\n"
        f"stdout tail:\n{result.stdout[-2500:]}\n"
        f"stderr tail:\n{result.stderr[-1500:]}"
    )
    assert result.returncode == 0, (
        f"--omnigent exited {result.returncode}. stderr tail:\n"
        f"{result.stderr[-2000:]}\nstdout tail:\n"
        f"{result.stdout[-1000:]}"
    )
    assert _CODEX_REGRESSION_CONTENT_MARKER in result.stdout, (
        f"Sentinel {_CODEX_REGRESSION_CONTENT_MARKER!r} missing "
        f"from stdout — codex didn't actually read the fixture "
        f"file. Either shell_tool is still disabled, or the "
        f"supervisor never delegated to the codex_worker.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )
