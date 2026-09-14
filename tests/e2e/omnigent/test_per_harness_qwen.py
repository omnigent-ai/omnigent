"""Phase 0 characterization test — qwen harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness qwen --model
<mock-model> -p "..."`` as a real subprocess against the mock LLM
server and snapshots structural observations (exit code, stderr
cleanliness, assistant text length).

**What breaks if this fails:**
- Omnigent' ``QwenExecutor`` regresses (the ``qwen --acp``
  subprocess lifecycle, the ACP JSON-RPC 2.0 event protocol).
- The ``qwen`` CLI binary disappears from PATH or changes its
  ``--acp`` startup contract.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing assistant text to stdout on turn complete.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
per-harness suite.

**History:** this test previously requested an ``omnigent_credentials_env``
fixture that PR #802 deleted (migrating every *other* per-harness e2e test —
antigravity, claude-sdk, codex, openai-agents-sdk, pi — to
``mock_credentials_env``). This one was missed, so it hard-errored at fixture
setup instead of running or skipping cleanly. Fixed by adopting the same
mock-LLM-server pattern as ``test_per_harness_codex.py`` — the qwen ACP
executor already honors ``OPENAI_BASE_URL`` for gateway routing (see
``docs/QWEN_FOLLOWUPS.md`` § Provider / gateway routing), so no new plumbing
was needed, only wiring this test up to the existing mechanism.
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

_HARNESS = "qwen"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves
# the turn produced a real model reply rather than an empty
# response or a pure error banner.
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. Qwen ACP mode spawns its own subprocess;
# 120s should be enough for init + first turn.
_RUN_TIMEOUT_SEC = 120


@pytest.fixture
def qwen_available() -> bool:
    """
    Availability probe for the qwen harness prerequisites.

    ``QwenExecutor`` shells out to the ``qwen`` CLI binary. Without
    it the executor raises immediately on session start. CI
    environments commonly lack the binary.

    :returns: True when ``qwen`` is on PATH.
    """
    return which("qwen") is not None


def test_per_harness_qwen_one_shot(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    qwen_available: bool,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness qwen -p <prompt>``
    exits 0 and emits a non-trivial assistant reply.

    Uses the mock LLM server (via ``OPENAI_BASE_URL`` in
    ``mock_credentials_env``) so the test runs without real API
    credentials or a Databricks workspace. The qwen ACP executor
    honors ``OPENAI_BASE_URL`` for its subprocess model routing.

    :param omnigent_python: Interpreter with omnigent
        installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the
        YAML spec and example tool modules resolve on sys.path.
    :param mock_credentials_env: Env vars pointing at the mock
        LLM server.
    :param mock_llm_server_url: Base URL of the mock server for
        configuring canned responses.
    :param qwen_available: True when the ``qwen`` CLI is present.
        On False the test skips — CI hosts commonly lack it.
    """
    if not qwen_available:
        pytest.skip(
            "qwen harness prerequisite missing: the 'qwen' CLI "
            "binary must be installed on PATH. Skipping — binary absent."
        )

    model = f"mock-harness-qwen-{uuid.uuid4().hex[:8]}"
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
    diffs = compare_snapshot("test_per_harness_qwen", observed)
    assert diffs == [], (
        "Snapshot mismatch for qwen run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # Separate assertion so a length regression names the length
    # check directly instead of being buried in the snapshot diff.
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"Qwen assistant text shorter than {_MIN_ASSISTANT_CHARS} "
        f"chars; got {observed['assistant_text']!r}"
    )
