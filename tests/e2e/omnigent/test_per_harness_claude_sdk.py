"""Phase 0 characterization test — claude-sdk harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness claude-sdk -p
"..."`` as a real subprocess against the mock LLM server and
snapshots the structural observations (exit code, stderr absence,
assistant text length).

**What breaks if this fails:**
- Omnigent' ``ClaudeSDKExecutor`` regresses (auth, MCP tool
  bridging, Claude Code binary discovery, or the message-stream
  translation in ``claude_sdk_executor.run_turn``).
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing the assistant text to stdout on turn complete.
- The Claude Agent SDK dependency or the ``claude`` CLI binary
  goes missing from the Omnigent venv.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
per-harness suite.

**Serial execution note:** These tests are designed for serial
execution — do NOT run them under pytest-xdist or any parallel
runner that shares the mock LLM server process. Each test uses a
UUID-keyed model name, so concurrent tests use separate queues and
queue cross-contamination is impossible even without ``reset_mock_llm``.
The ``reset_mock_llm`` call is kept as a safety guard to clear any
leftover state from prior test runs in the same session, but it
would wipe another test's queue if two tests ran simultaneously.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from shutil import which
from typing import Any

import pytest

from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_HARNESS = "claude-sdk"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length — anything longer than "hi" is
# enough to prove the turn actually produced model output (not
# an empty response or a pure error banner).
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. claude-sdk boots the Claude CLI plus an MCP
# bridge, so it's slower than openai-agents; 180s keeps headroom
# for cold starts on the CI host without letting a truly hung run
# pin the suite forever.
_RUN_TIMEOUT_SEC = 180


@pytest.fixture
def claude_sdk_available(omnigent_python: Path) -> bool:
    """
    Skip-guard for environments that can't run the claude-sdk
    harness.

    claude-sdk needs BOTH the Python package (inside the
    *omnigent* venv — the test's own venv is irrelevant because
    the test shells out) and the ``claude`` CLI binary on PATH.
    The binary is installed manually by users on their dev
    machines (``npm install -g @anthropic-ai/claude-code``), so
    CI environments commonly lack it.

    :param omnigent_python: The interpreter the subprocess
        uses. We probe THIS one for the ``claude_agent_sdk``
        import, not the current interpreter.
    :returns: True when both prerequisites are satisfied.
    """
    probe = subprocess.run(
        [
            str(omnigent_python),
            "-c",
            "import importlib.util, sys; "
            "sys.exit(0 if importlib.util.find_spec('claude_agent_sdk') else 1)",
        ],
        capture_output=True,
    )
    sdk_present = probe.returncode == 0
    cli_present = which("claude") is not None
    return sdk_present and cli_present


def test_per_harness_claude_sdk_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    claude_sdk_available: bool,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness claude-sdk -p
    <prompt>`` exits 0 and emits a non-trivial assistant reply.

    Uses the mock LLM server via ``ANTHROPIC_BASE_URL`` so the
    test runs without real Anthropic credentials. The mock server
    handles ``/v1/messages`` (the Anthropic-native endpoint) so
    the ClaudeSDKExecutor's requests are intercepted and answered
    with canned responses.

    :param omnigent_python: Interpreter with omnigent +
        claude-agent-sdk installed.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Env vars from the mock-LLM
        fixture (provides ``OPENAI_BASE_URL``; we add
        ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_API_KEY`` below).
    :param mock_llm_server_url: Base URL of the mock server for
        configuring canned responses and building
        ``ANTHROPIC_BASE_URL``.
    :param claude_sdk_available: True when the claude-sdk
        prerequisites (SDK package + ``claude`` binary) are
        present. If False, the test skips — the ``claude`` binary
        is a genuine proprietary CLI that CI commonly lacks.
    """
    if not claude_sdk_available:
        pytest.skip(
            "claude-sdk harness prerequisites missing: both the "
            "'claude_agent_sdk' Python package and the 'claude' CLI "
            "binary must be present on PATH. Skipping — binary absent."
        )

    model = f"mock-harness-claude-sdk-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello there, how are you today?"}],
        key=model,
    )

    # Build env: start from the mock env and add Anthropic-specific
    # vars so ClaudeSDKExecutor's /v1/messages calls land on the
    # mock server rather than api.anthropic.com.
    env = dict(mock_credentials_env)
    env["ANTHROPIC_BASE_URL"] = mock_llm_server_url
    env["ANTHROPIC_API_KEY"] = "mock-key"

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            model,
            "--harness",
            _HARNESS,
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    # Claude SDK on macOS prints a one-line sandbox-fallback
    # warning to stderr on every launch (Linux-only bwrap).
    # That's benign and orthogonal to this test; the observation
    # we care about is "no hard errors in stderr" which we check
    # by excluding the known-benign line before the assertion.
    stderr_stripped = "\n".join(
        line
        for line in result.stderr.splitlines()
        if "Could not apply default local CLI sandbox" not in line
    ).strip()
    # Assistant text lands on stdout. It may be prefixed by an
    # echo of the prompt ("You> say hi...") depending on CLI
    # mode; the length check below tolerates either form.
    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stderr_is_clean": stderr_stripped == "",
        # Trimmed because whitespace around LLM output is noisy
        # and not something we want the snapshot comparator to
        # trip on.
        "assistant_text": result.stdout.strip(),
    }

    # Full stderr surfaced on failure so CI logs show WHY the
    # run went wrong — stderr here is opaque unless we dump it.
    diffs = compare_snapshot("test_per_harness_claude_sdk", observed)
    assert diffs == [], (
        "Snapshot mismatch for claude-sdk run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # Separate assertion so the failure diagnostic names the
    # length-check directly instead of being buried in the
    # snapshot diff list.
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"Claude SDK assistant text shorter than "
        f"{_MIN_ASSISTANT_CHARS} chars; got "
        f"{observed['assistant_text']!r}"
    )
