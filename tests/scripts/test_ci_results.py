"""Exercise the final workflow checks with GitHub's needs/outputs interface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/ci/require_results.py"
CI_JOBS = (
    "pytest",
    "stores-postgres",
    "stores-cockroachdb",
    "stores-mysql",
    "omnidev",
    "codex-parity",
)
UI_JOBS = ("build-sidecar", "e2e-ui", "browser-contract")


def run_result(suite, needs, *, draft=False):
    return subprocess.run(
        [sys.executable, str(SCRIPT), suite],
        env=dict(os.environ, NEEDS=json.dumps(needs), IS_DRAFT=str(draft).lower()),
        text=True,
        capture_output=True,
        timeout=10,
    )


def full_needs(suite):
    if suite == "ci":
        selection, jobs = "scope", CI_JOBS
        outputs = dict.fromkeys(("backend", "database", "omnidev", "sidecar"), "true")
    elif suite in {"web", "docker"}:
        selection = "scope"
        jobs = ("web-test", "ios") if suite == "web" else ("build",)
        outputs = {"web": "true", "ios": "true"} if suite == "web" else {"docker": "true"}
    else:
        selection, jobs = "setup", UI_JOBS
        outputs = {"ui": "full"}
    return {
        selection: {"result": "success", "outputs": outputs},
        **{job: {"result": "success"} for job in jobs},
    }


@pytest.mark.parametrize(
    "suite,job",
    [("ci", job) for job in CI_JOBS]
    + [("ui", job) for job in UI_JOBS]
    + [("web", "web-test"), ("web", "ios"), ("docker", "build")],
)
@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "missing"])
def test_selected_consumer_must_succeed(suite, job, result):
    needs = full_needs(suite)
    if result == "missing":
        del needs[job]
    else:
        needs[job]["result"] = result
    process = run_result(suite, needs)
    assert process.returncode == 1
    assert job in process.stdout


@pytest.mark.parametrize(
    "suite,outputs,selected",
    [
        (
            "ci",
            {"database": "true", "backend": "true", "omnidev": "true", "sidecar": "true"},
            CI_JOBS,
        ),
        (
            "ci",
            {"database": "false", "backend": "false", "omnidev": "true", "sidecar": "false"},
            ("omnidev",),
        ),
        (
            "ci",
            {"database": "false", "backend": "false", "omnidev": "false", "sidecar": "true"},
            ("codex-parity",),
        ),
        (
            "ci",
            {"database": "false", "backend": "false", "omnidev": "true", "sidecar": "true"},
            ("omnidev", "codex-parity"),
        ),
        (
            "ci",
            {"database": "false", "backend": "false", "omnidev": "false", "sidecar": "false"},
            (),
        ),
        (
            "ci",
            {"database": "false", "backend": "true", "omnidev": "false", "sidecar": "false"},
            ("pytest",),
        ),
        ("ui", {"ui": "full"}, UI_JOBS),
        ("ui", {"ui": "dependency"}, UI_JOBS),
        ("web", {"web": "false", "ios": "true"}, ("ios",)),
        ("web", {"web": "true", "ios": "false"}, ("web-test",)),
        ("web", {"web": "false", "ios": "false"}, ()),
        ("docker", {"docker": "false"}, ()),
        ("docker", {"docker": "true"}, ("build",)),
        ("ui", {"ui": "sidecar"}, ("build-sidecar", "e2e-ui")),
        ("ui", {"ui": "none"}, ()),
    ],
)
def test_intentional_skips_allow_full_scoped_and_lint_only_runs(suite, outputs, selected):
    needs = full_needs(suite)
    selection = "setup" if suite == "ui" else "scope"
    needs[selection]["outputs"] = outputs
    for job in set(needs) - {selection}:
        needs[job]["result"] = "success" if job in selected else "skipped"
    process = run_result(suite, needs)
    assert process.returncode == 0, process.stdout + process.stderr


@pytest.mark.parametrize("suite", ["ci", "ui", "web", "docker"])
@pytest.mark.parametrize(
    "fault",
    ["skipped_selection", "failed_selection", "invalid_outputs", "missing_outputs", "draft"],
)
def test_selection_failure_is_not_an_intentional_skip(suite, fault):
    needs = full_needs(suite)
    selection = needs["setup" if suite == "ui" else "scope"]
    if fault.endswith("selection"):
        selection["result"] = "skipped" if fault.startswith("skipped") else "failure"
    elif fault == "invalid_outputs":
        selection["outputs"] = dict.fromkeys(selection["outputs"], "invalid")
    elif fault == "missing_outputs":
        selection["outputs"] = {}
    else:
        for job in needs.values():
            if "outputs" not in job:
                job["result"] = "skipped"
    process = run_result(suite, needs, draft=fault == "draft")
    assert (process.returncode == 0) == (fault == "draft"), process.stdout + process.stderr
