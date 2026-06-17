"""Phase 0 characterization test — qwen harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness qwen --model
<model> -p "..."`` as a real subprocess and snapshots structural
observations (exit code, stderr cleanliness, assistant text
length). Captured against current Omnigent; re-run unchanged
in later phases to prove the integration preserves behavior for
the qwen harness.

**What breaks if this fails:**
- Omnigent' ``QwenExecutor`` regresses (the ``qwen --mode rpc``
  subprocess lifecycle, the JSONL event protocol).
- The ``qwen`` CLI binary disappears from PATH or its
  ``--mode rpc`` subcommand changes its startup contract.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing assistant text to stdout on turn complete.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
per-harness suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._model_pools import resolve_model
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.omnigent._snapshot import compare_snapshot

# Model + harness are hardcoded because the test name advertises
# "qwen harness".
_MODEL = resolve_model("qwen/qwen-plus", key=__name__)
_HARNESS = "qwen"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves
# the turn produced a real model reply rather than an empty
# response or a pure error banner.
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. Qwen RPC mode spawns its own subprocess;
# 120s should be enough for init + first turn.
_RUN_TIMEOUT_SEC = 120

_pytest_qwen_unavailable = cli_unavailable_reason("qwen")
pytestmark = pytest.mark.skipif(
    _pytest_qwen_unavailable is not None,
    reason=(
        "qwen harness e2e requires a runnable 'qwen' CLI; "
        f"{_pytest_qwen_unavailable}. Install/fix Qwen to run this test."
    ),
)


def test_per_harness_qwen_one_shot(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    omnigent_credentials_env: dict[str, str],
) -> None:
    """
    ``omnigent run hello_world.yaml --harness qwen -p <prompt>``
    exits 0 and emits a non-trivial assistant reply.

    :param omnigent_python: Interpreter with omnigent
        installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the
        YAML spec and example tool modules resolve on sys.path.
    :param omnigent_credentials_env: Env vars with
        ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
        ``DATABRICKS_CONFIG_PROFILE`` populated from
        ``--llm-api-key``.
    """
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            _MODEL,
            "--harness",
            _HARNESS,
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=omnigent_credentials_env,
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
