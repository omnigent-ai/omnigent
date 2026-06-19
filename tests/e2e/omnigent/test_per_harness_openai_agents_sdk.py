"""Phase 0 characterization test — openai-agents-sdk harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness openai-agents
--model <mock-model> -p "..."`` as a real subprocess against the
mock LLM server and snapshots structural observations (exit code,
stderr cleanliness, assistant text length).

**What breaks if this fails:**
- Omnigent' ``OpenAIAgentsSDKExecutor`` regresses (the Runner
  lifecycle, the Responses-API adapter in
  ``omnigent.open_responses_sdk``, the MCP tool bridging, or
  the event stream translation to ``ExecutorEvent`` types).
- The ``openai-agents`` Python package (``agents`` module) is
  missing from the omnigent venv or its public API changes
  incompatibly.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing the assistant text on turn complete.

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
from typing import Any

import pytest

from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_HARNESS = "openai-agents"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves
# the turn produced a real model reply rather than an empty
# response or a pure error banner.
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. openai-agents runs inside the harness
# process (no external subprocess to boot) so it's faster than
# codex/claude-sdk, but 180s keeps headroom for cold starts on
# loaded CI hosts.
_RUN_TIMEOUT_SEC = 180


@pytest.fixture
def openai_agents_available(omnigent_python: Path) -> bool:
    """
    Availability probe for the openai-agents-sdk harness.

    ``OpenAIAgentsSDKExecutor`` imports the ``agents`` package
    lazily on first use. The package must be installed in the
    *omnigent* venv (the subprocess interpreter) — the current
    pytest interpreter is irrelevant because the test shells out.

    :param omnigent_python: Interpreter the subprocess will
        use. Probe THIS one for the ``agents`` import, not the
        current pytest interpreter.
    :returns: True when the ``agents`` package imports cleanly
        in the omnigent venv.
    """
    probe = subprocess.run(
        [
            str(omnigent_python),
            "-c",
            "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('agents') else 1)",
        ],
        capture_output=True,
    )
    return probe.returncode == 0


def test_per_harness_openai_agents_sdk_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    openai_agents_available: bool,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness openai-agents -p
    <prompt>`` exits 0 and emits a non-trivial assistant reply.

    Uses the mock LLM server so the test runs without real API
    credentials. The openai-agents executor honors
    ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` env vars directly
    (populated by ``mock_credentials_env``) — no
    ``~/.databrickscfg`` touch is required for this harness.

    :param omnigent_python: Interpreter with omnigent +
        ``openai-agents`` installed.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Env vars pointing at the mock
        LLM server.
    :param mock_llm_server_url: Base URL of the mock server for
        configuring canned responses.
    :param openai_agents_available: True when the ``agents``
        package is importable in the omnigent venv. On False
        the test skips — consistent with the codex and
        claude-sdk harness tests that skip when their binary
        is absent.
    """
    if not openai_agents_available:
        pytest.skip(
            "openai-agents-sdk harness prerequisite missing: "
            "the 'agents' Python package (openai-agents) must be "
            "installed in the Omnigent venv. Skipping — package absent."
        )

    model = f"mock-harness-openai-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello there, how are you today?"}],
        key=model,
    )

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
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stderr_is_clean": result.stderr.strip() == "",
        # Trimmed because whitespace around LLM output is noisy
        # and not something we want the snapshot comparator to
        # trip on.
        "assistant_text": result.stdout.strip(),
    }

    # Full stderr surfaced on failure so CI logs show WHY the run
    # went wrong — stderr here is opaque unless we dump it.
    diffs = compare_snapshot(
        "test_per_harness_openai_agents_sdk",
        observed,
    )
    assert diffs == [], (
        "Snapshot mismatch for openai-agents run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # Separate assertion so a length regression names the length
    # check directly instead of being buried in the snapshot diff.
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"openai-agents assistant text shorter than "
        f"{_MIN_ASSISTANT_CHARS} chars; got "
        f"{observed['assistant_text']!r}"
    )
