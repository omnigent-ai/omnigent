"""Per-harness live characterization test — databricks-genie harness, one-shot.

Runs ``omnigent run hello_world.yaml --harness databricks-genie --model
<space_id> -p "..."`` as a real subprocess and asserts structural invariants
(exit 0, a non-trivial assistant reply). This is the end-to-end gate for the
databricks-genie harness: CLI parse → spec materialize → spawn the
``databricks-genie`` harness subprocess →
:class:`~omnigent.inner.databricks_genie_executor.DatabricksGenieExecutor`
streaming a remote Genie space over the Agent-mode Responses API
(``POST /api/2.0/genie/agents/{agent_id}/responses``) → ``TurnComplete`` → the
``-p`` one-shot printer.

**Prerequisites (skipped when absent):**
- The ``databricks-sdk`` package installed (the ``databricks`` extra) — it
  resolves the workspace credentials the request's bearer auth refreshes.
- ``OMNIGENT_GENIE_SPACE_ID`` set to a real Genie space id.
- A resolvable Databricks credential — typically ``databricks auth login``
  having written ``~/.databrickscfg`` (optionally named via
  ``OMNIGENT_GENIE_PROFILE`` → ``DATABRICKS_CONFIG_PROFILE``).
- The workspace preview toggle for Genie **Agent mode** enabled. The APIs are
  Beta; without it the endpoint answers 404 ``FEATURE_DISABLED``. This one
  cannot be detected up front — it only shows up as a failed run — so a run
  that reports it is skipped rather than failed (see below).

**Why this test cannot use the mock LLM server:** the Genie Responses endpoint
is OpenAI-shaped but lives at a workspace-specific Databricks URL derived from
the resolved credentials, so it does not honour ``OPENAI_BASE_URL``. The harness
can only be exercised against a real workspace, so the test **skips** (rather
than fails) when the prerequisites are absent so the e2e shards stay green; it
runs for real wherever they are present.
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

# Outlast the executor's stream idle timeout (900s of wire silence) plus process
# startup, so a wedged stream surfaces as the executor's actionable error rather
# than a truncated subprocess.
_RUN_TIMEOUT_SEC = 960

# Substrings in a failed run that mean "the environment isn't ready", not "the
# harness is broken" — the workspace preview toggle for Agent mode (Beta) is off,
# or no Databricks credential could be minted. Neither is detectable up front.
_UNAVAILABLE_MARKERS = (
    "FEATURE_DISABLED",
    "Agent-mode APIs are Beta",
    "Databricks authentication failed",
)


def test_per_harness_databricks_genie_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
) -> None:
    """``omnigent run ... --harness databricks-genie --model <space> -p <q>`` works.

    :param omnigent_python: Interpreter with omnigent installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the YAML spec resolves.
    """
    try:
        # find_spec("databricks.sdk") imports the parent package first, so a
        # machine without the extra raises here rather than returning None.
        databricks_sdk_missing = importlib.util.find_spec("databricks.sdk") is None
    except ModuleNotFoundError:
        databricks_sdk_missing = True
    if databricks_sdk_missing:
        pytest.skip(
            "databricks-genie prerequisite missing: the 'databricks-sdk' package is "
            "not installed (install the 'databricks' extra), so no workspace "
            "credential can be resolved for the Responses request."
        )
    space_id = os.environ.get("OMNIGENT_GENIE_SPACE_ID", "").strip()
    if not space_id:
        pytest.skip(
            "databricks-genie prerequisite missing: OMNIGENT_GENIE_SPACE_ID is not set. "
            "Set it to a real Genie space id (and authenticate via 'databricks auth "
            "login') to run this live gate."
        )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    env = os.environ.copy()
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

    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        marker = next((m for m in _UNAVAILABLE_MARKERS if m in combined), None)
        if marker is not None:
            pytest.skip(
                f"databricks-genie prerequisite missing: the run reported {marker!r}. "
                "Enable the workspace preview for Genie Agent mode (Beta) and "
                "authenticate via 'databricks auth login' to run this live gate."
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
