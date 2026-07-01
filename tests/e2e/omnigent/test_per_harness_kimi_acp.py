"""Per-harness characterization test — kimi-acp harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness kimi-acp --model <model>
-p "..."`` as a real subprocess and snapshots structural observations
(exit code, stderr cleanliness, assistant text length). Captured against
current Omnigent; re-run unchanged in later phases to prove the integration
preserves behavior for the kimi-acp harness.

**What breaks if this fails:**
- Omnigent's ``KimiAcpExecutor`` regresses (the ``kimi acp`` subprocess
  lifecycle, the ACP JSON-RPC 2.0 event protocol, or the
  ``agent_message_chunk`` / ``agent_thought_chunk`` translation).
- The ``kimi`` CLI binary disappears from PATH or changes its ``acp``
  startup contract.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path stops printing
  assistant text to stdout on turn complete.
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
# "kimi-acp harness".
_MODEL = resolve_model("kimi-k2-turbo", key=__name__)
_HARNESS = "kimi-acp"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves the turn
# produced a real model reply rather than an empty response or an error banner.
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. kimi acp mode spawns its own subprocess; 120s should be
# enough for init + first turn.
_RUN_TIMEOUT_SEC = 120

_pytest_kimi_unavailable = cli_unavailable_reason("kimi")
pytestmark = pytest.mark.skipif(
    _pytest_kimi_unavailable is not None,
    reason=(
        "kimi-acp harness e2e requires a runnable 'kimi' CLI; "
        f"{_pytest_kimi_unavailable}. Install/log in to Kimi Code to run this test."
    ),
)


def test_per_harness_kimi_acp_one_shot(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    omnigent_credentials_env: dict[str, str],
) -> None:
    """
    ``omnigent run hello_world.yaml --harness kimi-acp -p <prompt>`` exits 0
    and emits a non-trivial assistant reply.

    :param omnigent_python: Interpreter with omnigent installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the YAML spec and
        example tool modules resolve on sys.path.
    :param omnigent_credentials_env: Env vars populated from ``--llm-api-key``
        (unused by kimi-acp, which authenticates via ``kimi login``, but passed
        for parity with the other per-harness tests).
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
        "assistant_text": result.stdout.strip(),
    }

    diffs = compare_snapshot("test_per_harness_kimi_acp", observed)
    assert diffs == [], (
        "Snapshot mismatch for kimi-acp run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"kimi-acp assistant text shorter than {_MIN_ASSISTANT_CHARS} "
        f"chars; got {observed['assistant_text']!r}"
    )
