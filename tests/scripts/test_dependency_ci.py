"""Exercise dependency selection on real Git merges and its workflow consumers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github/scripts/ci"
OMNIDEV = "dev/omnidev/Cargo.lock"
SIDECAR = "tests/codex_parity/sidecar/Cargo.lock"
HOOKS = ".pre-commit-config.yaml"


@pytest.fixture
def merged_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    for name in (OMNIDEV, SIDECAR, HOOKS, "pnpm-lock.yaml", "web/package.json", "uv.lock"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "dependency")
    return repo, git


def run_selection(tmp_path, repo, head, merge, *, event_name="pull_request"):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": head}}}))
    output = tmp_path / "output"
    process = subprocess.run(
        [sys.executable, str(CI / "dependency_scope.py")],
        cwd=repo,
        env=dict(
            os.environ,
            GITHUB_EVENT_NAME=event_name,
            GITHUB_EVENT_PATH=str(event),
            GITHUB_SHA=merge,
            GITHUB_OUTPUT=str(output),
            GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize(
    ("paths", "backend", "omnidev", "sidecar", "ui"),
    [
        ([OMNIDEV], False, True, False, "none"),
        ([SIDECAR], False, False, True, "sidecar"),
        ([OMNIDEV, SIDECAR], False, True, True, "sidecar"),
        ([HOOKS], False, False, False, "none"),
        (["pnpm-lock.yaml", "web/package.json"], False, False, False, "full"),
        ([OMNIDEV, "pnpm-lock.yaml"], False, True, False, "full"),
        ([SIDECAR, HOOKS], False, False, True, "sidecar"),
        ([SIDECAR, "uv.lock"], True, True, True, "full"),
        (["omnigent/runner/app.py"], True, True, True, "full"),
        (
            [".github/actions/setup-harness-cli/locks/codex/package-lock.json"],
            True,
            True,
            True,
            "full",
        ),
        ([".github/workflows/ci.yml"], True, True, True, "full"),
    ],
)
def test_merge_diff_selects_union_and_drives_existing_matrices(
    tmp_path, merged_repo, paths, backend, omnidev, sidecar, ui
):
    repo, git = merged_repo
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("after\n")
    git("add", ".")
    git("commit", "-qm", "update")
    head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("merge", "--no-ff", "-qm", "merge", "dependency")
    merge = git("rev-parse", "HEAD")
    # Match actions/checkout's shallow merge checkout; diff needs only its parents.
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", "--depth=2", repo.as_uri(), str(checkout)], check=True)
    scope = run_selection(tmp_path, checkout, head, merge)
    assert {key: scope[key] for key in ("backend", "omnidev", "sidecar", "ui")} == {
        "backend": str(backend).lower(),
        "omnidev": str(omnidev).lower(),
        "sidecar": str(sidecar).lower(),
        "ui": ui,
    }
    for script in ("e2e-shard-matrix.sh", "integration-matrix.sh"):
        output = tmp_path / script
        subprocess.run(
            ["bash", str(CI / script)],
            env=dict(
                os.environ,
                GITHUB_OUTPUT=str(output),
                EVENT_NAME="pull_request",
                RUN_TESTS=scope["backend"],
                NUM_SHARDS="4",
            ),
            check=True,
        )
        matrix = json.loads(output.read_text().split("=", 1)[1])
        assert bool(matrix["include"]) == backend


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_head",
        "wrong_merge",
        "no_merge",
        "deleted",
        "renamed",
        "empty",
        "push",
        "schedule",
        "workflow_dispatch",
    ],
)
def test_unknown_or_non_pr_input_keeps_full_coverage(tmp_path, merged_repo, fault):
    repo, git = merged_repo
    if fault == "deleted":
        (repo / SIDECAR).unlink()
    elif fault == "renamed":
        git("mv", SIDECAR, "tests/codex_parity/sidecar/renamed.lock")
    elif fault != "empty":
        (repo / SIDECAR).write_text("after\n")
    git("add", ".")
    git("commit", "--allow-empty", "-qm", "update")
    head = git("rev-parse", "HEAD")
    if fault != "no_merge":
        git("checkout", "-q", "main")
        git("merge", "--no-ff", "-qm", "merge", "dependency")
    merge = git("rev-parse", "HEAD")
    scope = run_selection(
        tmp_path,
        repo,
        "a" * 40 if fault == "wrong_head" else head,
        "b" * 40 if fault == "wrong_merge" else merge,
        event_name=fault if fault in {"push", "schedule", "workflow_dispatch"} else "pull_request",
    )
    assert scope["backend"] == scope["omnidev"] == scope["sidecar"] == "true"
    assert scope["ui"] == "full"


def test_workflows_connect_selection_to_jobs():
    workflows = {
        name: yaml.safe_load((ROOT / ".github/workflows" / f"{name}.yml").read_text())
        for name in ("ci", "e2e", "e2e-ui", "integration")
    }
    for name, workflow in workflows.items():
        setup = workflow["jobs"]["scope" if name == "ci" else "setup"]
        checkout = next(step for step in setup["steps"] if "checkout@" in step.get("uses", ""))
        assert checkout["with"]["fetch-depth"] == 2
        assert setup["needs"] == "gate"
        assert any(
            step.get("uses") == "./.github/actions/dependency-ci-scope" for step in setup["steps"]
        )
    jobs = workflows["ci"]["jobs"]
    for name in ("pytest", "stores-postgres", "stores-cockroachdb", "stores-mysql"):
        assert jobs[name]["needs"] == "scope"
        assert "needs.scope.outputs.backend == 'true'" in jobs[name]["if"]
    for name, output in (("omnidev", "omnidev"), ("codex-parity", "sidecar")):
        assert f"needs.scope.outputs.{output} == 'true'" in jobs[name]["if"]
    ui = workflows["e2e-ui"]["jobs"]
    shard = next(step for step in ui["setup"]["steps"] if step.get("id") == "matrix")
    assert "steps.scope.outputs.ui == 'sidecar'" in shard["env"]["NUM_SHARDS"]
    assert "needs.setup.outputs.ui == 'full'" in ui["browser-contract"]["if"]
    run = next(step for step in ui["e2e-ui"]["steps"] if step.get("name") == "Run UI e2e tests")
    assert run["env"]["UI_SCOPE"] == "${{ needs.setup.outputs.ui }}"
    assert "--ui-codex-sidecar-only" in run["run"]


@pytest.mark.parametrize(
    "fault", ["none", "omnidev", "parity", "ui", "scope", "queued", "cancelled", "setup"]
)
def test_merge_gate_waits_for_selected_rust_checks(tmp_path, fault):
    checks = {
        "DCO": ("completed", "success"),
        "Pre-commit checks": ("completed", "success"),
        "CI scope": ("completed", "success"),
        "Rust tests (omnidev)": ("completed", "success"),
        "Pytest (codex-parity)": ("completed", "success"),
        "E2E UI Tests (shard 0/2)": ("completed", "success"),
        "E2E UI Tests (shard 1/2)": ("completed", "success"),
    }
    if fault in {"omnidev", "parity", "ui", "scope"}:
        name = {
            "omnidev": "Rust tests (omnidev)",
            "parity": "Pytest (codex-parity)",
            "ui": "E2E UI Tests (shard 1/2)",
            "scope": "CI scope",
        }[fault]
        checks[name] = ("completed", "failure")
    run_status, conclusion = "completed", "success"
    if fault == "queued":
        del checks["Pytest (codex-parity)"]
        run_status, conclusion = "in_progress", "null"
    elif fault in {"cancelled", "setup"}:
        conclusion = "cancelled" if fault == "cancelled" else "failure"
    api = {
        "checks": "\n".join(
            f"{name}\t{status}\t{result}\t2026-10-19T10:00:00Z"
            for name, (status, result) in checks.items()
        ),
        "runs": "\n".join(
            f"{name}\t{run_status}\t{conclusion}\t2026-10-19T10:00:00Z"
            for name in ("CI", "E2E Tests", "E2E UI Tests", "Integration Tests")
        ),
    }
    fixtures = tmp_path / "api.json"
    fixtures.write_text(json.dumps(api))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "api = json.loads(Path(os.environ['TEST_GATE_API']).read_text())\n"
        "print(api['checks' if '/check-runs' in sys.argv[2] else 'runs'])\n"
    )
    gh.chmod(0o755)
    process = subprocess.run(
        ["bash", str(ROOT / ".github/scripts/merge-ready/evaluate-checks.sh")],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            TEST_GATE_API=str(fixtures),
            REPO="example/project",
            SHA="a" * 40,
            GITHUB_OUTPUT=str(tmp_path / "gate-output"),
        ),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert (process.returncode == 0) == (fault == "none"), process.stdout + process.stderr
