"""Exercise dependency selection on real Git merges and its workflow consumers."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github/scripts/ci"
OMNIDEV = "dev/omnidev/Cargo.lock"
SIDECAR = "tests/codex_parity/sidecar/Cargo.lock"
HOOKS = ".pre-commit-config.yaml"


def test_merge_gate_skip_membership_with_a_large_check_list():
    """A matching check stays skippable when the list exceeds the pipe buffer."""
    subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            'source "$1"; printf -v padding "%100000s" ""; ALLOW_SKIP+=("$padding"); '
            'is_allow_skip "Pytest (inner-rest)"; ! is_allow_skip "unknown check"',
            "bash",
            str(ROOT / ".github/scripts/merge-ready/required.sh"),
        ],
        check=True,
    )


@pytest.fixture
def scope_module():
    spec = importlib.util.spec_from_file_location("dependency_scope", CI / "dependency_scope.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ECOSYSTEM_CHANGES = {
    "uv": ("python", {"uv.lock": ("version = 1\n", "version = 2\n")}),
    "npm": (
        "javascript",
        {
            "web/package.json": (
                '{"dependencies":{"pkg":"^1.0.0"}}',
                '{"dependencies":{"pkg":"^1.0.1"}}',
            ),
            "pnpm-lock.yaml": ("before", "after"),
        },
    ),
    "github-actions": (
        "actions",
        {
            ".github/workflows/ci.yml": (
                "steps:\n  - uses: actions/checkout@" + "a" * 40 + " # v6\n",
                "steps:\n  - uses: actions/checkout@" + "b" * 40 + " # v7\n",
            )
        },
    ),
    "cargo": (
        "omnidev",
        {
            "dev/omnidev/Cargo.toml": (
                '[dependencies]\ntokio = "1.0.0"\n',
                '[dependencies]\ntokio = "1.0.1"\n',
            ),
            OMNIDEV: ("before", "after"),
        },
    ),
    "gradle": (
        "android",
        {
            "web/android/gradle/libs.versions.toml": (
                '[versions]\nagp = "9.1.1"',
                '[versions]\nagp = "9.4.1"',
            )
        },
    ),
    "bundler": ("ios", {"web/ios/Gemfile.lock": ("old fastlane lock", "new fastlane lock")}),
    "docker": (
        "docker",
        {
            "deploy/docker/Dockerfile": (
                "FROM python:3.12-slim\nRUN python --version\n",
                "FROM python:3.13-slim\nRUN python --version\n",
            )
        },
    ),
    "pre-commit": (
        "hooks",
        {
            HOOKS: (
                "repos:\n  - repo: https://example.test/hooks\n    rev: v1.0.0\n",
                "repos:\n  - repo: https://example.test/hooks\n    rev: v1.0.1\n",
            )
        },
    ),
}


def test_every_configured_ecosystem_has_a_dependency_profile():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert {update["package-ecosystem"] for update in config["updates"]} == set(ECOSYSTEM_CHANGES)


@pytest.mark.parametrize("ecosystem", ECOSYSTEM_CHANGES)
def test_each_ecosystem_selects_focused_coverage_from_a_real_merge(
    tmp_path, merged_repo, ecosystem
):
    repo, git = merged_repo
    expected, changes = ECOSYSTEM_CHANGES[ecosystem]
    git("checkout", "main")
    for name, (before, _after) in changes.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(before)
    git("add", ".")
    git("commit", "-qm", "fixture baseline")
    git("checkout", "dependency")
    git("merge", "--ff-only", "main")
    for name, (_before, after) in changes.items():
        (repo / name).write_text(after)
    git("add", ".")
    git("commit", "-qm", "dependency update")
    head = git("rev-parse", "HEAD")
    git("checkout", "main")
    git("merge", "--no-ff", "-qm", "test merge", "dependency")
    selection = run_selection(tmp_path, repo, head, git("rev-parse", "HEAD"))
    assert selection["dependency"] == "true", selection
    assert selection["reason"] == f"Dependency scopes: {expected}"
    assert selection["database"] == str(ecosystem in {"uv", "github-actions"}).lower()
    assert selection["backend"] == str(ecosystem in {"uv", "github-actions"}).lower()
    assert selection["windows"] == selection["backend"]
    assert selection["ios"] == str(ecosystem == "bundler").lower()
    assert selection["web"] == str(ecosystem in {"npm", "github-actions"}).lower()
    assert (
        selection["docker"] == str(ecosystem in {"uv", "npm", "github-actions", "docker"}).lower()
    )
    groups = {row["group"] for row in json.loads(selection["python_matrix"])["include"]}
    assert ("runtime-core" in groups) == (ecosystem in {"uv", "github-actions"})
    expected_groups = json.loads((CI / "python-matrix.json").read_text())
    assert json.loads(selection["python_matrix"])["include"] == (
        expected_groups if ecosystem in {"uv", "github-actions"} else []
    )


@pytest.mark.parametrize(
    "path,before,after",
    [
        (
            "web/package.json",
            '{"dependencies":{"pkg":"1"},"scripts":{"build":"vite"}}',
            '{"dependencies":{"pkg":"2"},"scripts":{"build":"vite build"}}',
        ),
        (
            "pyproject.toml",
            '[project]\ndependencies=["pkg>=1"]\n[project.scripts]\nomni="pkg:main"',
            '[project]\ndependencies=["pkg>=2"]\n[project.scripts]\nomni="pkg:other"',
        ),
        (
            "dev/omnidev/Cargo.toml",
            '[dependencies]\ntokio={version="1",features=["fs"]}',
            '[dependencies]\ntokio={version="2",features=["process"]}',
        ),
        (
            HOOKS,
            "repos:\n  - repo: https://example.test/a\n    rev: v1",
            "repos:\n  - repo: https://example.test/b\n    rev: v2",
        ),
        (
            ".github/workflows/ci.yml",
            "steps:\n  - uses: actions/checkout@v6\n  - run: echo one",
            "steps:\n  - uses: actions/checkout@v7\n  - run: echo two",
        ),
        (
            "deploy/docker/Dockerfile",
            "FROM python:3.12\nRUN python --version",
            "FROM python:3.13\nRUN python -V",
        ),
    ],
)
def test_configuration_changes_keep_full_coverage(scope_module, path, before, after):
    selection = scope_module.select_scope(
        [{"filename": path, "status": "modified"}], read=lambda _: (before, after)
    )
    assert selection["dependency"] == "false"
    assert selection["ui"] == "full"
    assert selection["database"] == "true"
    assert json.loads(selection["python_matrix"])["include"] == json.loads(
        (CI / "python-matrix.json").read_text()
    )


def test_gradle_wrapper_bundle_keeps_the_android_profile(scope_module):
    paths = [
        "web/android/gradlew",
        "web/android/gradlew.bat",
        "web/android/gradle/wrapper/gradle-wrapper.jar",
        "web/android/gradle/wrapper/gradle-wrapper.properties",
    ]
    selection = scope_module.select_scope(
        [{"filename": path, "status": "modified"} for path in paths]
    )
    assert selection["dependency"] == "true"
    assert selection["reason"] == "Dependency scopes: android"
    assert selection["ui"] == "none"


@pytest.mark.parametrize("changed", ["sqlalchemy", "greenlet", "typing-extensions", "unrelated"])
def test_python_lock_updates_keep_all_database_and_image_checks(scope_module, changed):
    before = """
[[package]]
name = "sqlalchemy"
version = "2.0.0"
dependencies = [{name = "greenlet"}]
[[package]]
name = "greenlet"
version = "3.0.0"
dependencies = [{name = "typing-extensions"}]
[[package]]
name = "typing-extensions"
version = "4.0.0"
[[package]]
name = "unrelated"
version = "1.0.0"
"""
    after = before.replace(f'name = "{changed}"\nversion = "', f'name = "{changed}"\nversion = "9')
    selection = scope_module.select_scope(
        [{"filename": "uv.lock", "status": "modified"}], read=lambda _: (before, after)
    )
    assert selection["database"] == "true"
    assert selection["docker"] == "true"
    assert selection["e2e"] == "true"


def test_python_manifest_changes_retain_database_and_image_checks(scope_module):
    selection = scope_module.select_scope(
        [{"filename": "pyproject.toml", "status": "modified"}],
        read=lambda _: (
            '[project]\ndependencies=["demo>=1"]',
            '[project]\ndependencies=["demo>=2"]',
        ),
    )
    assert selection["dependency"] == selection["database"] == selection["docker"] == "true"


@pytest.mark.parametrize("section", ["catalog", "catalogs", "overrides", "settings"])
def test_workspace_versions_are_distinct_from_configuration(scope_module, section):
    before = f"packages:\n  - web\n{section}:\n  pkg: 1.0.0\n"
    after = before.replace("1.0.0", "1.0.1")
    selection = scope_module.select_scope(
        [{"filename": "pnpm-workspace.yaml", "status": "modified"}],
        read=lambda _: (before, after),
    )
    assert selection["dependency"] == str(section != "settings").lower()


def test_configured_container_manifests_have_consumer_coverage(scope_module):
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    update = next(row for row in config["updates"] if row["package-ecosystem"] == "docker")
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-build.yml").read_text())
    triggers = workflow[True]["pull_request"]["paths"]
    for directory in update["directories"]:
        for path in (ROOT / directory.lstrip("/")).glob("Dockerfile*"):
            if path.suffix == ".dockerignore":
                continue
            name = path.relative_to(ROOT).as_posix()
            assert (
                scope_module.file_scope(name, read=lambda _: ("FROM demo:1", "FROM demo:2"))
                == "docker"
            )
            assert name in triggers or f"{path.parent.relative_to(ROOT)}/**" in triggers
            assert (
                name in (ROOT / ".github/workflows/docker-build.yml").read_text()
                or "cloudflare" in name
            )


def test_cross_ecosystem_bundle_retains_each_consumer(scope_module):
    changes = {
        path: values
        for _scope, files in ECOSYSTEM_CHANGES.values()
        for path, values in files.items()
    }
    selection = scope_module.select_scope(
        [{"filename": path, "status": "modified"} for path in changes], read=changes.__getitem__
    )
    assert selection["dependency"] == "true"
    assert all(
        selection[key] == "true"
        for key in (
            "backend",
            "e2e",
            "integration",
            "windows",
            "ios",
            "web",
            "docker",
            "omnidev",
            "sidecar",
        )
    )
    assert selection["ui"] == "full"


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
        path.write_text(
            ' {"dependencies": {"pkg": "^1.0.0"}}\n'
            if name.endswith("package.json")
            else "repos:\n  - rev: v1.0.0\n"
            if name == HOOKS
            else 'version = 1\n[[package]]\nname = "demo"\nversion = "1.0.0"\n'
            if name == "uv.lock"
            else "before\n"
        )
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
    ("paths", "backend", "e2e", "omnidev", "sidecar", "ui"),
    [
        ([OMNIDEV], False, False, True, False, "none"),
        ([SIDECAR], False, False, False, True, "sidecar"),
        ([OMNIDEV, SIDECAR], False, False, True, True, "sidecar"),
        ([HOOKS], False, False, False, False, "none"),
        (["pnpm-lock.yaml"], False, True, False, False, "full"),
        (["pnpm-lock.yaml", "web/package.json"], False, True, False, False, "full"),
        ([OMNIDEV, "pnpm-lock.yaml"], False, True, True, False, "full"),
        ([SIDECAR, HOOKS], False, False, False, True, "sidecar"),
        ([SIDECAR, "uv.lock"], True, True, False, True, "full"),
        (["omnigent/runner/app.py"], True, True, True, True, "full"),
        (
            [".github/actions/setup-harness-cli/locks/codex/package-lock.json"],
            True,
            True,
            True,
            True,
            "full",
        ),
        ([".github/workflows/ci.yml"], True, True, True, True, "full"),
    ],
)
def test_merge_diff_selects_union_and_drives_existing_matrices(
    tmp_path, merged_repo, paths, backend, e2e, omnidev, sidecar, ui
):
    repo, git = merged_repo
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ' {"dependencies": {"pkg": "^1.0.1"}}\n'
            if name.endswith("package.json")
            else "repos:\n  - rev: v1.0.1\n"
            if name == HOOKS
            else 'version = 1\n[[package]]\nname = "demo"\nversion = "1.0.1"\n'
            if name == "uv.lock"
            else "after\n"
        )
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
    assert {key: scope[key] for key in ("backend", "e2e", "omnidev", "sidecar", "ui")} == {
        "backend": str(backend).lower(),
        "e2e": str(e2e).lower(),
        "omnidev": str(omnidev).lower(),
        "sidecar": str(sidecar).lower(),
        "ui": ui,
    }
    for script, selected in (
        ("e2e-shard-matrix.sh", e2e),
        ("integration-matrix.sh", backend),
    ):
        output = tmp_path / script
        subprocess.run(
            ["bash", str(CI / script)],
            env=dict(
                os.environ,
                GITHUB_OUTPUT=str(output),
                EVENT_NAME="pull_request",
                RUN_TESTS=str(selected).lower(),
                NUM_SHARDS="4",
            ),
            check=True,
        )
        matrix = json.loads(output.read_text().split("=", 1)[1])
        assert bool(matrix["include"]) == selected


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
    assert scope["backend"] == scope["e2e"] == scope["omnidev"] == scope["sidecar"] == "true"
    assert scope["ui"] == "full"


def test_workflows_connect_selection_to_jobs():
    workflows = {
        name: yaml.safe_load((ROOT / ".github/workflows" / f"{name}.yml").read_text())
        for name in ("ci", "e2e", "e2e-ui", "integration", "web-tests", "docker-build", "windows")
    }
    for name, workflow in workflows.items():
        setup = workflow["jobs"]["setup" if name in {"e2e", "e2e-ui", "integration"} else "scope"]
        checkout = next(step for step in setup["steps"] if "checkout@" in step.get("uses", ""))
        assert checkout["with"]["fetch-depth"] == 2
        assert setup["needs"] == "gate"
        assert any(
            step.get("uses") == "./.github/actions/dependency-ci-scope" for step in setup["steps"]
        )
    assert workflows["ci"]["jobs"]["scope"]["name"] == "CI coverage scope"
    assert workflows["e2e"]["jobs"]["setup"]["name"] == "E2E coverage scope"
    assert workflows["e2e-ui"]["jobs"]["setup"]["name"] == "UI coverage scope"
    assert workflows["web-tests"]["jobs"]["scope"]["name"] == "Web coverage scope"
    web_steps = workflows["web-tests"]["jobs"]["web-test"]["steps"]
    checkouts = [step for step in web_steps if "checkout@" in step.get("uses", "")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["fetch-depth"] == 2
    peer_check = next(
        step for step in web_steps if step.get("name") == "Validate changed dependency peers"
    )
    assert "needs.scope.outputs.dependency == 'true'" in peer_check["if"]
    assert "check_npm_peers.py --base HEAD^1" in peer_check["run"]
    e2e_steps = workflows["e2e"]["jobs"]["e2e"]["steps"]
    e2e_run = next(step for step in e2e_steps if step.get("uses") == "./.github/actions/e2e-run")
    action = yaml.safe_load((ROOT / ".github/actions/e2e-run/action.yml").read_text())
    assert (
        e2e_run["with"].get("dependency_smoke", action["inputs"]["dependency_smoke"]["default"])
        == "false"
    )
    matrix = next(
        step for step in workflows["e2e"]["jobs"]["setup"]["steps"] if step.get("id") == "matrix"
    )
    assert matrix["env"]["NUM_SHARDS"] == "8"
    jobs = workflows["ci"]["jobs"]
    sidecar_cache = next(
        step for step in jobs["codex-parity"]["steps"] if step.get("id") == "sidecar-cache"
    )
    assert sidecar_cache["with"]["path"] == ".tmp-codex-parity-target"
    for job, manifest in (
        ("omnidev", "dev/omnidev/Cargo.toml"),
        ("codex-parity", "tests/codex_parity/sidecar/Cargo.toml"),
    ):
        rust_test = next(
            step for step in jobs[job]["steps"] if "cargo test --locked" in step.get("run", "")
        )
        assert manifest in rust_test["run"]
        assert "if" not in rust_test
    python_install = next(
        step for step in jobs["pytest"]["steps"] if step.get("name") == "Install dependencies"
    )
    assert "--extra openshell --extra kubernetes" in python_install["run"]
    sdk_check = next(
        step
        for step in jobs["pytest"]["steps"]
        if step.get("name") == "Check installed sandbox SDK consumers"
    )
    assert sdk_check["if"] == "matrix.group == 'server-rest'"
    assert "kubernetes._ensure_sdk()" in sdk_check["run"]
    assert "openshell._ensure_sdk()" in sdk_check["run"]
    for name in ("pytest", "stores-postgres", "stores-cockroachdb", "stores-mysql"):
        assert jobs[name]["needs"] == "scope"
        output = "backend" if name == "pytest" else "database"
        assert f"needs.scope.outputs.{output} == 'true'" in jobs[name]["if"]
    for name, output in (("omnidev", "omnidev"), ("codex-parity", "sidecar")):
        assert f"needs.scope.outputs.{output} == 'true'" in jobs[name]["if"]
    for name, output in (("e2e", "e2e"), ("integration", "integration")):
        matrix = next(
            step
            for step in workflows[name]["jobs"]["setup"]["steps"]
            if step.get("id") == "matrix"
        )
        assert matrix["env"]["RUN_TESTS"] == "${{ steps.scope.outputs." + output + " }}"
    ui = workflows["e2e-ui"]["jobs"]
    shard = next(step for step in ui["setup"]["steps"] if step.get("id") == "matrix")
    assert "steps.scope.outputs.ui != 'full'" in shard["env"]["NUM_SHARDS"]
    assert "needs.setup.outputs.ui == 'full'" in ui["browser-contract"]["if"]
    run = next(step for step in ui["e2e-ui"]["steps"] if step.get("name") == "Run UI e2e tests")
    assert run["env"]["UI_SCOPE"] == "${{ needs.setup.outputs.ui }}"
    assert "--ui-codex-sidecar-only" in run["run"]
    for workflow, selection, result_name, suite in (
        (workflows["ci"], "scope", "CI result", "ci"),
        (workflows["e2e-ui"], "setup", "E2E UI result", "ui"),
        (workflows["web-tests"], "scope", "Web result", "web"),
        (workflows["docker-build"], "scope", "Docker result", "docker"),
    ):
        result = workflow["jobs"]["result"]
        assert result["name"] == result_name
        assert result["if"] == "always()"
        test_jobs = set(workflow["jobs"]) - {"gate", selection, "result", "coverage-report"}
        assert set(result["needs"]) == test_jobs | {selection}
        # Reject a failed security/selection prerequisite before checking out PR code.
        guard = result["steps"][0]
        assert guard["env"]["SELECTION_RESULT"] == "${{ needs." + selection + ".result }}"
        for status in ("success", "failure", "skipped", "cancelled"):
            process = subprocess.run(
                ["bash", "-e", "-c", guard["run"]],
                env=dict(os.environ, SELECTION_RESULT=status),
                check=False,
            )
            assert (process.returncode == 0) == (status == "success")
        assert "checkout@" in result["steps"][1]["uses"]
        validate = result["steps"][-1]
        assert validate["env"]["NEEDS"] == "${{ toJSON(needs) }}"
        assert validate["run"] == f"python3 .github/scripts/ci/require_results.py {suite}"


def _run_merge_gate(
    tmp_path: Path, snapshots: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    """Run evaluate-checks.sh against a fake ``gh`` serving API snapshots.

    Each read of the check runs advances to the next snapshot (the last one
    repeats), so a test can model checks that GitHub updates between reads.
    """
    fixtures = tmp_path / "api.json"
    fixtures.write_text(json.dumps(snapshots))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "snapshots = json.loads(Path(os.environ['TEST_GATE_API']).read_text())\n"
        "reads = Path(os.environ['TEST_GATE_API'] + '.reads')\n"
        "count = int(reads.read_text()) if reads.exists() else 0\n"
        "if '/check-runs' in sys.argv[2]:\n"
        "    count += 1\n"
        "    reads.write_text(str(count))\n"
        "api = snapshots[min(max(count, 1), len(snapshots)) - 1]\n"
        "response = api['checks' if '/check-runs' in sys.argv[2] else 'runs']\n"
        "subprocess.run(['jq', '-r', sys.argv[sys.argv.index('--jq')+1]], "
        "input=json.dumps(response), text=True, check=True)\n"
    )
    gh.chmod(0o755)
    return subprocess.run(
        ["bash", str(ROOT / ".github/scripts/merge-ready/evaluate-checks.sh")],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            TEST_GATE_API=str(fixtures),
            REPO="example/project",
            SHA="a" * 40,
            GITHUB_OUTPUT=str(tmp_path / "gate-output"),
            MERGE_READY_LAG_DELAY_S="0",
        ),
        text=True,
        capture_output=True,
        timeout=30,
    )


def _check_reads(tmp_path: Path) -> int:
    return int((tmp_path / "api.json.reads").read_text())


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "omnidev",
        "parity",
        "ui",
        "scope",
        "queued",
        "cancelled",
        "setup",
        "skipped_result",
        "missing_result",
        "old_workflows",
        "legacy_missing_result",
        "legacy_missing_ui_result",
        "queued_retry",
        "missing_web_result",
        "failed_web_result",
        "missing_docker_result",
        "skipped_docker_result",
        "android",
    ],
)
def test_merge_gate_waits_for_selected_rust_checks(tmp_path, fault):
    checks = {
        "DCO": ("completed", "success"),
        "Pre-commit checks": ("completed", "success"),
        "CI coverage scope": ("completed", "success"),
        "CI result": ("completed", "success"),
        "UI coverage scope": ("completed", "success"),
        "E2E UI result": ("completed", "success"),
        "Web coverage scope": ("completed", "success"),
        "Web result": ("completed", "success"),
        "Docker scope": ("completed", "success"),
        "Docker result": ("completed", "success"),
        "Build unsigned AAB": ("completed", "success"),
        "Rust tests (omnidev)": ("completed", "success"),
        "Pytest (codex-parity)": ("completed", "success"),
        "E2E UI Tests (shard 0/2)": ("completed", "success"),
        "E2E UI Tests (shard 1/2)": ("completed", "success"),
    }
    if fault in {"omnidev", "parity", "ui", "scope", "android"}:
        name = {
            "omnidev": "Rust tests (omnidev)",
            "parity": "Pytest (codex-parity)",
            "ui": "E2E UI Tests (shard 1/2)",
            "scope": "CI coverage scope",
            "android": "Build unsigned AAB",
        }[fault]
        checks[name] = ("completed", "failure")
    run_status, conclusion = "completed", "success"
    if fault == "queued":
        del checks["Pytest (codex-parity)"]
        run_status, conclusion = "in_progress", "null"
    elif fault in {"cancelled", "setup"}:
        conclusion = "cancelled" if fault == "cancelled" else "failure"
    elif fault == "skipped_result":
        checks["CI result"] = ("completed", "skipped")
    elif fault == "missing_result":
        del checks["E2E UI result"]
    elif fault == "missing_web_result":
        del checks["Web result"]
    elif fault == "failed_web_result":
        checks["Web result"] = ("completed", "failure")
    elif fault == "missing_docker_result":
        del checks["Docker result"]
    elif fault == "skipped_docker_result":
        checks["Docker result"] = ("completed", "skipped")
    elif fault.startswith("legacy_missing_"):
        checks["CI scope"] = checks.pop("CI coverage scope")
        checks["UI scope"] = checks.pop("UI coverage scope")
        del checks["E2E UI result" if fault == "legacy_missing_ui_result" else "CI result"]
    elif fault == "old_workflows":
        for name in (
            "CI coverage scope",
            "CI result",
            "UI coverage scope",
            "E2E UI result",
            "Rust tests (omnidev)",
        ):
            del checks[name]
    api = {
        "checks": {
            "check_runs": [
                {
                    "id": index,
                    "name": name,
                    "status": status,
                    "conclusion": result,
                    "completed_at": "2026-10-19T10:00:00Z",
                }
                for index, (name, (status, result)) in enumerate(checks.items(), start=1)
            ]
        },
        "runs": {
            "workflow_runs": [
                {
                    "name": name,
                    "status": run_status,
                    "conclusion": conclusion,
                    "created_at": "2026-10-19T10:00:00Z",
                }
                for name in ("CI", "E2E Tests", "E2E UI Tests", "Integration Tests")
            ]
        },
    }
    if fault == "queued_retry":
        api["checks"]["check_runs"].append(
            {
                "id": 100,
                "name": "Pytest (codex-parity)",
                "status": "queued",
                "conclusion": None,
                "started_at": None,
                "completed_at": None,
            }
        )
    process = _run_merge_gate(tmp_path, [api])
    assert (process.returncode == 0) == (fault in {"none", "old_workflows"}), (
        process.stdout + process.stderr
    )


_E2E_SHARDS = [
    f"E2E Tests (shard {index}/{count})" for count in (2, 4, 8) for index in range(count)
]


def _gate_snapshot(
    shard_status: str,
    e2e_runs: tuple[tuple[str, str], ...] = (("completed", "2026-10-19T10:00:00Z"),),
    failed: str | None = None,
) -> dict[str, Any]:
    """One API snapshot in which only ``E2E Tests (shard 7/8)`` may be unfinished.

    :param shard_status: Status of that shard's check run.
    :param e2e_runs: ``(status, created_at)`` for each E2E Tests workflow run.
    :param failed: A required check to report as completed with a failure.
    """
    checks = dict.fromkeys(["DCO", "Pre-commit checks", *_E2E_SHARDS], "completed")
    checks["E2E Tests (shard 7/8)"] = shard_status
    runs = [
        {
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-19T10:00:00Z",
        }
        for name in ("CI", "E2E UI Tests", "Integration Tests")
    ] + [
        {
            "name": "E2E Tests",
            "status": status,
            "conclusion": "success" if status == "completed" else None,
            "created_at": created_at,
        }
        for status, created_at in e2e_runs
    ]
    return {
        "checks": {
            "check_runs": [
                {
                    "id": index,
                    "name": name,
                    "status": status,
                    "conclusion": (
                        ("failure" if name == failed else "success")
                        if status == "completed"
                        else None
                    ),
                }
                for index, (name, status) in enumerate(checks.items(), start=1)
            ]
        },
        "runs": {"workflow_runs": runs},
    }


def test_merge_gate_rereads_a_check_that_lags_its_completed_workflow(tmp_path):
    """A completed workflow's check that still reads as running is re-read, not failed."""
    lagging = _gate_snapshot("in_progress")
    settled = _gate_snapshot("completed")
    process = _run_merge_gate(tmp_path, [lagging, lagging, settled])
    assert process.returncode == 0, process.stdout + process.stderr
    assert _check_reads(tmp_path) == 3
    assert "All required checks green" in process.stdout


@pytest.mark.parametrize("shard_id", range(8))
def test_merge_gate_blocks_each_failed_eight_shard_check(tmp_path, shard_id):
    name = f"E2E Tests (shard {shard_id}/8)"
    process = _run_merge_gate(tmp_path, [_gate_snapshot("completed", failed=name)])
    assert process.returncode == 1, process.stdout + process.stderr
    assert name in (tmp_path / "gate-output").read_text()


def test_merge_gate_reports_red_after_the_lag_rereads_run_out(tmp_path):
    process = _run_merge_gate(tmp_path, [_gate_snapshot("in_progress")])
    assert process.returncode == 1, process.stdout + process.stderr
    assert _check_reads(tmp_path) == 6
    assert "E2E Tests (shard 7/8)" in (tmp_path / "gate-output").read_text()


@pytest.mark.parametrize(
    "snapshot",
    [
        _gate_snapshot("in_progress", (("in_progress", "2026-10-19T10:00:00Z"),)),
        _gate_snapshot("in_progress", failed="Pre-commit checks"),
        _gate_snapshot(
            "in_progress",
            (("completed", "2026-10-19T10:00:00Z"), ("in_progress", "2026-10-19T10:05:00Z")),
        ),
    ],
    ids=["workflow-running", "with-a-failed-check", "newest-run-running"],
)
def test_merge_gate_reports_red_without_rereading_when_a_red_check_is_not_lagging(
    tmp_path, snapshot
):
    process = _run_merge_gate(tmp_path, [snapshot])
    assert process.returncode == 1, process.stdout + process.stderr
    assert _check_reads(tmp_path) == 1


def test_merge_gate_follows_the_newest_workflow_run_when_rereading(tmp_path):
    """An older running attempt does not stop a re-read once the newest run completed."""
    runs = (("in_progress", "2026-10-19T10:00:00Z"), ("completed", "2026-10-19T10:05:00Z"))
    process = _run_merge_gate(
        tmp_path, [_gate_snapshot("in_progress", runs), _gate_snapshot("completed", runs)]
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert _check_reads(tmp_path) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "valid",
        "incompatible",
        "missing",
        "optional",
        "baseline",
        "context",
        "prerelease",
        "prerelease-outside-range",
    ],
)
def test_changed_npm_peer_gate_uses_candidate_lock_and_npm_semver(merged_repo, fault):
    repo, git = merged_repo
    lock = {
        "lockfileVersion": "9.0",
        "packages": {"storybook@1.0.0": {"peerDependencies": {"vite": "^8.0.0"}}},
        "snapshots": {"storybook@1.0.0": {"dependencies": {"vite": "8.1.0"}}},
    }
    if fault == "baseline":
        lock["snapshots"]["storybook@1.0.0"]["dependencies"]["vite"] = "7.0.0"
    path = repo / "pnpm-lock.yaml"
    path.write_text(yaml.safe_dump(lock))
    git("add", ".")
    git("commit", "-qm", "baseline peers")
    if fault != "baseline":
        old = "storybook@1.0.0"
        new = old if fault == "context" else "storybook@1.0.1"
        metadata = lock["packages"].pop(old)
        snapshot = lock["snapshots"].pop(old)
        lock["packages"][new] = metadata
        lock["snapshots"][new] = snapshot
        if fault in {"incompatible", "context"}:
            snapshot["dependencies"]["vite"] = "7.0.0"
        elif fault in {"missing", "optional"}:
            snapshot["dependencies"].clear()
            if fault == "optional":
                metadata["peerDependenciesMeta"] = {"vite": {"optional": True}}
        elif fault.startswith("prerelease"):
            snapshot["dependencies"]["vite"] = (
                "8.1.0-rc.1" if fault == "prerelease" else "9.0.0-rc.1"
            )
    path.write_text(yaml.safe_dump(lock))
    git("add", ".")
    git("commit", "--allow-empty", "-qm", "update peers")
    process = subprocess.run(
        [sys.executable, str(CI / "check_npm_peers.py"), "--base", "HEAD^1"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert (process.returncode == 0) == (
        fault in {"valid", "optional", "baseline", "prerelease"}
    ), process.stdout + process.stderr
