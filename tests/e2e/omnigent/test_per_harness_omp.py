"""Phase 0 characterization test — omp harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness omp --model
<mock-model> -p "..."`` as a real subprocess against the mock LLM
server and snapshots structural observations (exit code, stderr
cleanliness, assistant text length).

**What breaks if this fails:**
- Omnigent' ``OmpExecutor`` regresses (the ``omp --mode rpc``
  subprocess lifecycle, the JSONL event protocol, the TCP
  ``_ToolServer`` that proxies tool calls back to Python, or
  the generated JavaScript extension that registers
  Omnigent tools with ``pi.registerTool()``).
- The ``omp`` CLI binary disappears from PATH or its
  ``--mode rpc`` subcommand changes its startup contract.
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

**Mock routing note (omp):** The omp executor translates the ambient
mock credential (``OPENAI_API_KEY``/``OPENAI_BASE_URL``) into a
generated ``models.yml`` gateway pointing at the mock server, and
registers the ``--model`` id under the routed provider — the same
shape as the pi harness's ``models.json`` path. If a particular omp
build read ``~/.databrickscfg`` instead and ignored the gateway
transport, the test would connect to a real endpoint rather than
the mock server and fail or behave non-deterministically. The
module-level ``pytestmark`` skips the test when ``omp`` is absent;
on CI the binary should either be absent (skip) or honor the
gateway transport.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_HARNESS = "omp"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves
# the turn produced a real model reply rather than an empty
# response or a pure error banner.
_MIN_ASSISTANT_CHARS = 4

# Subprocess timeout. omp spawns a JS subprocess with its own
# init path and registers tools via the generated extension
# before accepting the first turn — slower than openai-agents,
# comparable to claude-sdk. 180s matches the other slow-harness
# tests.
_RUN_TIMEOUT_SEC = 180

_pytest_omp_unavailable = cli_unavailable_reason("omp")
pytestmark = pytest.mark.skipif(
    _pytest_omp_unavailable is not None,
    reason=(
        "omp harness e2e requires a runnable 'omp' CLI; "
        f"{_pytest_omp_unavailable}. Install/fix Oh My Pi to run this test."
    ),
)


def test_per_harness_omp_one_shot(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness omp -p <prompt>``
    exits 0 and emits a non-trivial assistant reply.

    Uses the mock LLM server so the test runs without real API
    credentials or a Databricks workspace. The omp executor routes
    model calls through the generated ``models.yml`` gateway
    (translated from the ambient mock credential).

    :param omnigent_python: Interpreter with omnigent
        installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the
        YAML spec and example tool modules resolve on sys.path.
    :param mock_credentials_env: Env vars pointing at the mock
        LLM server.
    :param mock_llm_server_url: Base URL of the mock server for
        configuring canned responses.
    """
    model = f"mock-harness-omp-{uuid.uuid4().hex[:8]}"
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
    diffs = compare_snapshot("test_per_harness_omp", observed)
    assert diffs == [], (
        "Snapshot mismatch for omp run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # Separate assertion so a length regression names the length
    # check directly instead of being buried in the snapshot diff.
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"omp assistant text shorter than {_MIN_ASSISTANT_CHARS} "
        f"chars; got {observed['assistant_text']!r}"
    )
