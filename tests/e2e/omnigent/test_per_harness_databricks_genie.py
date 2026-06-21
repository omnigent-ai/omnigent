"""Per-harness live characterization test — databricks-genie harness, one-shot.

Runs ``omnigent run hello_world.yaml --harness databricks-genie --model
<space_id> -p "..."`` as a real subprocess and asserts structural invariants
(exit 0, a non-trivial assistant reply). This is the end-to-end gate for the
databricks-genie harness: CLI parse → spec materialize → spawn the
``databricks-genie`` harness subprocess →
:class:`~omnigent.inner.databricks_genie_executor.DatabricksGenieExecutor`
driving a remote Genie space over the ``databricks-sdk`` Genie API →
``TurnComplete`` → the ``-p`` one-shot printer.

**Prerequisites (skipped when absent):**
- The ``databricks-sdk`` package installed (the ``databricks`` extra).
- ``OMNIGENT_GENIE_SPACE_ID`` set to a real Genie space id.
- A resolvable Databricks credential — typically ``databricks auth login``
  having written ``~/.databrickscfg`` (optionally named via
  ``OMNIGENT_GENIE_PROFILE`` → ``DATABRICKS_CONFIG_PROFILE``).

**Why this test cannot use the mock LLM server:** Genie is a proprietary
Databricks conversational API reached through ``WorkspaceClient.genie``; it does
not honour ``OPENAI_BASE_URL``. The harness can only be exercised against a real
workspace, so the test **skips** (rather than fails) when the prerequisites are
absent so the e2e shards stay green; it runs for real wherever they are present.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_HARNESS = "databricks-genie"
_PROMPT = "Give me a one-sentence summary of what this space can answer."

# Minimum assistant-text length proving a genuine Genie reply (not empty/error).
_MIN_ASSISTANT_CHARS = 4

# Genie cold-starts a conversation and may run a warehouse query; allow headroom.
_RUN_TIMEOUT_SEC = 300


def test_per_harness_databricks_genie_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
) -> None:
    """``omnigent run ... --harness databricks-genie --model <space> -p <q>`` works.

    :param omnigent_python: Interpreter with omnigent installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the YAML spec resolves.
    """
    if importlib.util.find_spec("databricks.sdk") is None:
        pytest.skip(
            "databricks-genie prerequisite missing: the 'databricks-sdk' package is "
            "not installed (install the 'databricks' extra)."
        )
    space_id = os.environ.get("OMNIGENT_GENIE_SPACE_ID", "").strip()
    if not space_id:
        pytest.skip(
            "databricks-genie prerequisite missing: OMNIGENT_GENIE_SPACE_ID is not set. "
            "Set it to a real Genie space id (and authenticate via 'databricks auth "
            "login') to run this live gate."
        )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    env = dict(os.environ)
    profile = os.environ.get("OMNIGENT_GENIE_PROFILE", "").strip()
    if profile:
        env["DATABRICKS_CONFIG_PROFILE"] = profile

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--harness",
            _HARNESS,
            "--model",
            space_id,
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

    assistant_text = result.stdout.strip()
    assert result.returncode == 0, (
        f"databricks-genie run exited {result.returncode}.\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert len(assistant_text) >= _MIN_ASSISTANT_CHARS, (
        f"databricks-genie assistant text shorter than {_MIN_ASSISTANT_CHARS} chars; "
        f"got {assistant_text!r}\n\nstderr:\n{result.stderr!r}"
    )
