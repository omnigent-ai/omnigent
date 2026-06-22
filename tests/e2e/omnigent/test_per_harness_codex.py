"""Phase 0 characterization test — codex harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness codex --model
<mock-model> -p "..."`` as a real subprocess against the mock LLM
server and snapshots structural observations (exit code, stderr
cleanliness, assistant text length).

**What breaks if this fails:**
- Omnigent' ``CodexExecutor`` regresses (``codex app-server``
  subprocess orchestration, App Server JSON-RPC protocol, the
  message-stream translation in ``codex_executor.run_turn``).
- The ``codex`` CLI binary disappears from PATH or its
  ``app-server`` subcommand changes its startup contract.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing assistant text to stdout on turn complete.

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

_HARNESS = "codex"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves
# the turn produced a genuine model reply (not an empty response
# or a pure error banner from the codex app-server).
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. codex boots its app-server subprocess and
# establishes the JSON-RPC stream before the first turn event,
# so it's slower than openai-agents; 180s matches claude-sdk's
# headroom and keeps pace with cold-start latency on CI hosts.
_RUN_TIMEOUT_SEC = 180


@pytest.fixture
def codex_available() -> bool:
    """
    Availability probe for the codex harness prerequisites.

    ``CodexExecutor`` shells out to the ``codex`` CLI binary
    (installed via ``npm i -g @openai/codex`` typically).
    Without it the executor raises immediately on session start.
    CI environments commonly lack the binary.

    :returns: True when ``codex`` is on PATH.
    """
    return which("codex") is not None


def test_per_harness_codex_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    codex_available: bool,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness codex -p <prompt>``
    exits 0 and emits a non-trivial assistant reply.

    Uses the mock LLM server (via ``OPENAI_BASE_URL`` in
    ``mock_credentials_env``) so the test runs without real API
    credentials or a Databricks workspace. The codex executor
    honors ``OPENAI_BASE_URL`` for its app-server model routing.

    :param omnigent_python: Interpreter with omnigent
        installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the
        YAML spec and example tool modules resolve on sys.path.
    :param mock_credentials_env: Env vars pointing at the mock
        LLM server.
    :param mock_llm_server_url: Base URL of the mock server for
        configuring canned responses.
    :param codex_available: True when the ``codex`` CLI is
        present. On False the test skips — codex is a genuine
        proprietary binary that CI typically lacks.
    """
    if not codex_available:
        pytest.skip(
            "codex harness prerequisite missing: the 'codex' CLI "
            "binary must be installed on PATH (install via "
            "'npm i -g @openai/codex'). Skipping — binary absent."
        )

    model = f"mock-harness-codex-{uuid.uuid4().hex[:8]}"
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

    # codex's app-server can print its own one-line startup
    # diagnostics to stderr before the first turn event. Known
    # benign lines (e.g. the ``App server listening`` banner) are
    # excluded before the cleanliness assertion so the test
    # doesn't spuriously fail on harmless informational output.
    stderr_stripped = "\n".join(
        line
        for line in result.stderr.splitlines()
        # Codex prints a Node runtime deprecation line on some
        # Node installs; orthogonal to the behavior under test.
        if "DeprecationWarning" not in line and "App server listening" not in line
    ).strip()

    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stderr_is_clean": stderr_stripped == "",
        # Trimmed because whitespace around LLM output is noisy
        # and not something we want the snapshot comparator to
        # trip on.
        "assistant_text": result.stdout.strip(),
    }

    # Full stderr surfaced on failure so CI logs show WHY the run
    # went wrong (e.g. missing binary) — stderr here is opaque
    # unless we dump it in the failure message.
    diffs = compare_snapshot("test_per_harness_codex", observed)
    assert diffs == [], (
        "Snapshot mismatch for codex run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # Separate assertion so a length regression names the length
    # check directly instead of being buried in the snapshot diff.
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"Codex assistant text shorter than {_MIN_ASSISTANT_CHARS} "
        f"chars; got {observed['assistant_text']!r}"
    )
