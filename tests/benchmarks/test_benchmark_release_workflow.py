"""Run the release benchmark's "Compare results" step against the real compare.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.benchmarks.test_compare import _journey, _skipped_journey

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/benchmark-release.yml").read_text())
COMPARE_STEP = next(s for s in WORKFLOW["jobs"]["benchmark"]["steps"] if s.get("id") == "compare")

_MEASURED = _journey([100, 101, 102], [120, 125, 130])
_REGRESSED = _journey([300, 301, 302], [320, 325, 330])


def _run_compare_step(
    tmp_path: Path, baseline: dict, candidate: dict, require: str, uv_body: str | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    if not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("Workflow execution requires bash and jq")
    (tmp_path / "baseline.json").write_text(json.dumps(baseline))
    (tmp_path / "candidate.json").write_text(json.dumps(candidate))
    workdir = tmp_path / "candidate"
    workdir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `uv run --no-sync <script> ARGS` → the real compare.py with this interpreter.
    uv = bin_dir / "uv"
    uv.write_text(
        uv_body or f'#!/usr/bin/env bash\nexec "{sys.executable}" "{REPO}/$3" "${{@:4}}"\n'
    )
    uv.chmod(0o755)
    output = tmp_path / "github_output"
    output.touch()
    # The step's literal env (thresholds); expressions are filled in below.
    step_env = {k: str(v) for k, v in COMPARE_STEP["env"].items() if "${{" not in str(v)}
    env = {
        **os.environ,
        **step_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "REQUIRE_JOURNEYS": require,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", COMPARE_STEP["run"]],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return result, outputs


@pytest.mark.parametrize(
    ("candidate", "require", "expected"),
    [
        pytest.param(
            {"interrupt": _MEASURED, "list_sessions": _MEASURED},
            "interrupt",
            {"regression": "false", "complete": "true", "unmeasured": ""},
            id="measured-no-regression",
        ),
        pytest.param(
            {"interrupt": _skipped_journey(), "list_sessions": _MEASURED},
            "interrupt",
            {"regression": "false", "complete": "false", "unmeasured": "interrupt"},
            id="unmeasured-only",
        ),
        pytest.param(
            {"interrupt": _skipped_journey(), "list_sessions": _REGRESSED},
            "interrupt",
            {"regression": "true", "complete": "false", "unmeasured": "interrupt"},
            id="regression-and-unmeasured",
        ),
        pytest.param(
            {"interrupt": _MEASURED, "list_sessions": _REGRESSED},
            "",
            {"regression": "true", "complete": "true", "unmeasured": ""},
            id="release-run-without-required-journeys",
        ),
    ],
)
def test_compare_step_outputs(
    tmp_path: Path, candidate: dict, require: str, expected: dict[str, str]
) -> None:
    baseline = {"journeys": {"interrupt": _MEASURED, "list_sessions": _MEASURED}}

    result, outputs = _run_compare_step(tmp_path, baseline, {"journeys": candidate}, require)

    assert result.returncode == 0, result.stderr
    assert outputs == expected


@pytest.mark.parametrize(
    ("uv_body", "error"),
    [
        pytest.param("exit 1", "compare.py failed (exit 1)", id="crash-without-report"),
        pytest.param(
            "echo report > ../comparison.md",
            "compare.py exited 0 without writing its JSON report",
            id="no-json-report",
        ),
    ],
)
def test_compare_step_fails_when_compare_writes_no_report(
    tmp_path: Path, uv_body: str, error: str
) -> None:
    report = {"journeys": {"interrupt": _MEASURED}}

    result, outputs = _run_compare_step(
        tmp_path, report, report, "interrupt", uv_body=f"#!/usr/bin/env bash\n{uv_body}\n"
    )

    assert result.returncode != 0
    assert error in result.stdout
    assert outputs == {}
