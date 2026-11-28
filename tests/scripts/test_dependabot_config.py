"""Offline policy checks for ``.github/dependabot.yml``.

A YAML syntax check cannot tell whether the config keeps the promises its
header makes: individual PRs except for cross-pinned MLflow packages, every
lockfile declared once at its root, and every declared path actually present.
Dependabot only validates the file after it is on ``main``, so these assertions
are the pre-merge gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".github/dependabot.yml"


@pytest.fixture(scope="module")
def entries() -> list[dict[str, Any]]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert config["version"] == 2
    return config["updates"]


def _directories(entry: dict[str, Any]) -> list[str]:
    assert ("directory" in entry) ^ ("directories" in entry), entry["package-ecosystem"]
    return entry.get("directories") or [entry["directory"]]


def _version_updates_on(entry: dict[str, Any]) -> bool:
    return entry.get("open-pull-requests-limit", 5) > 0


def test_every_entry_has_a_schedule(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        assert entry["schedule"]["interval"] in {"daily", "weekly", "monthly"}, entry


def test_every_declared_directory_exists(entries: list[dict[str, Any]]) -> None:
    """A path Dependabot cannot find is a silent no-op, not an error it reports."""
    for entry in entries:
        for directory in _directories(entry):
            relative = directory.strip("/")
            matches = (
                [REPO_ROOT]
                if not relative
                else [p for p in REPO_ROOT.glob(relative) if p.is_dir()]
            )
            assert matches, f"{entry['package-ecosystem']}: {directory} matches no directory"


def test_each_ecosystem_directory_is_declared_once(entries: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        for directory in _directories(entry):
            key = (entry["package-ecosystem"], directory)
            assert key not in seen, f"{key} declared twice"
            seen.add(key)


def test_lockfiles_are_declared_at_their_workspace_roots(entries: list[dict[str, Any]]) -> None:
    """pnpm and uv keep one root lockfile; a member entry would update a manifest
    without it (pre-commit then fails on the mismatch) and file duplicate PRs."""
    npm_dirs = [d for e in entries if e["package-ecosystem"] == "npm" for d in _directories(e)]
    assert "/" in npm_dirs
    members = yaml.safe_load((REPO_ROOT / "pnpm-workspace.yaml").read_text())["packages"]
    for member in members:
        assert f"/{member}" not in npm_dirs, f"pnpm member {member} declared as its own npm root"

    assert not [e for e in entries if e["package-ecosystem"] == "pip"], (
        "uv.lock needs `uv`, not pip"
    )
    uv_dirs = [d for e in entries if e["package-ecosystem"] == "uv" for d in _directories(e)]
    assert uv_dirs == ["/"]


def test_only_cross_pinned_mlflow_packages_are_grouped(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        groups = entry.get("groups") or {}
        if entry["package-ecosystem"] != "uv":
            assert not groups, f"{entry['package-ecosystem']}: updates must have individual PRs"
            continue
        assert len(groups) == 2
        assert {group.get("applies-to", "version-updates") for group in groups.values()} == {
            "version-updates",
            "security-updates",
        }
        for group in groups.values():
            assert set(group["patterns"]) == {"mlflow", "mlflow-skinny", "mlflow-tracing"}
            assert not group.get("exclude-patterns")
            assert not group.get("update-types"), "MLflow must also resolve together for majors"


def test_disabled_entries_ignore_everything(entries: list[dict[str, Any]]) -> None:
    """An entry kept only to silence the security toggle must not bump anything."""
    for entry in entries:
        if _version_updates_on(entry):
            continue
        assert {"dependency-name": "*"} in (entry.get("ignore") or []), entry


def test_web_workspace_never_receives_major_bumps(entries: list[dict[str, Any]]) -> None:
    (npm_root,) = [
        e for e in entries if e["package-ecosystem"] == "npm" and _directories(e) == ["/"]
    ]
    assert {"dependency-name": "*", "update-types": ["version-update:semver-major"]} in (
        npm_root.get("ignore") or []
    )


def test_active_ecosystems_share_the_seven_day_cooldown(entries: list[dict[str, Any]]) -> None:
    """Mirrors pnpm's minimumReleaseAge and uv's exclude-newer so all three agree."""
    for entry in entries:
        if _version_updates_on(entry):
            assert entry.get("cooldown", {}).get("default-days") == 7, entry["package-ecosystem"]


def test_github_actions_cover_the_composite_actions(entries: list[dict[str, Any]]) -> None:
    (actions,) = [e for e in entries if e["package-ecosystem"] == "github-actions"]
    assert set(_directories(actions)) >= {"/", "/.github/actions/*"}


def test_active_ecosystems_carry_labels_and_commit_prefix(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        if not _version_updates_on(entry):
            continue
        assert "dependencies" in entry.get("labels", []), entry["package-ecosystem"]
        assert entry.get("commit-message", {}).get("prefix") == "chore(deps)", entry


def test_workspace_coverage_maps_to_real_jobs_and_steps():
    import json

    policy = json.loads((REPO_ROOT / ".github/dependabot-automerge.json").read_text())
    for importer, coverage in policy["npm"]["consumers"].items():
        assert (REPO_ROOT / importer / "package.json").is_file()
        for consumer in coverage:
            assert consumer["workflow"] in policy["workflow_test_jobs"], consumer
            workflow = yaml.safe_load(
                (REPO_ROOT / ".github/workflows" / consumer["workflow"]).read_text()
            )
            jobs = [job for job in workflow["jobs"].values() if job.get("name") == consumer["job"]]
            assert len(jobs) == 1, consumer
            assert not jobs[0].get("continue-on-error"), consumer
            steps = [step for step in jobs[0]["steps"] if step.get("name") == consumer["step"]]
            assert len(steps) == 1 and not steps[0].get("continue-on-error"), consumer


@pytest.mark.parametrize(
    "path,allowed",
    [
        ("uv.lock", True),
        ("pnpm-lock.yaml", True),
        ("tests/codex_parity/sidecar/Cargo.lock", True),
        (".github/actions/setup-uv/action.yml", True),
        (".github/workflows/ci.yml", True),
        (".github/workflows/ui-snapshot-update.yml", True),
        (".github/workflows/release-omnigent.yml", False),
        (".github/workflows/benchmark-pr.yml", False),
    ],
)
def test_app_refresh_permissions_cover_optional_and_reusable_workflows(monkeypatch, path, allowed):
    monkeypatch.syspath_prepend(str(REPO_ROOT / ".github/scripts"))
    from dependabot_policy import NeedsReview, require_refresh_permissions

    workflows = {
        str(workflow.relative_to(REPO_ROOT)): workflow.read_text()
        for workflow in (REPO_ROOT / ".github/workflows").iterdir()
        if workflow.suffix in {".yml", ".yaml"}
    }
    if allowed:
        require_refresh_permissions(workflows, {path}, "dependabot/uv/ruff")
    else:
        with pytest.raises(NeedsReview, match="automatic branch refresh") as raised:
            require_refresh_permissions(workflows, {path}, "dependabot/uv/ruff")
        assert path in str(raised.value)


@pytest.mark.parametrize(
    "name", ["oss-regen-on-comment.yml", "oss-regenerate-and-smoke.yml", "dependabot-regen.yml"]
)
def test_regeneration_preserves_pins_and_skips_install_scripts(name):
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows" / name).read_text())
    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    regeneration = [
        step["run"] for step in steps if "pnpm install --lockfile-only" in step.get("run", "")
    ]
    assert len(regeneration) == 1
    assert "pnpm install --lockfile-only --ignore-scripts" in regeneration[0]
    assert not any("rm -f pnpm-lock.yaml" in step.get("run", "") for step in steps)
    if name != "oss-regenerate-and-smoke.yml":
        setup = next(step for step in steps if "/setup-uv@" in step.get("uses", ""))
        assert setup["with"]["version"] == "0.13.4"


def _workflow(name: str) -> tuple[str, dict[str, Any]]:
    text = (REPO_ROOT / ".github/workflows" / name).read_text()
    workflow = yaml.safe_load(text)
    # PyYAML reads the bare `on:` key as the boolean True.
    workflow["on"] = workflow.pop(True, workflow.get("on"))
    return text, workflow


def test_dependabot_regen_runs_dependency_code_without_any_credential():
    """The job that runs `uv lock` / pnpm and the PR's own actions gets no
    secret and no write scope, so code from an updated dependency has nothing
    to steal; its only way out is the lockfile artifact."""
    text, workflow = _workflow("dependabot-regen.yml")
    # `pull_request` (not `pull_request_target`): GitHub runs a Dependabot push
    # with a read-only token and a cache scoped to the PR ref.
    assert set(workflow["on"]) == {"pull_request"}
    assert not re.search(r"\$\{\{[^}]*\bsecrets\b", text)
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"contents": "read"}
    checkout = next(step for step in job["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False
    (upload,) = [
        step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", "")
    ]
    assert upload["with"]["name"] == "dependabot-regen-lockfiles"


def test_dependabot_regen_publish_runs_no_pr_code():
    """The job that holds the push token runs on a fresh runner from main and
    only moves the two lockfiles from the artifact onto the PR branch."""
    _, regen = _workflow("dependabot-regen.yml")
    text, workflow = _workflow("dependabot-regen-publish.yml")
    assert workflow["on"] == {
        "workflow_run": {"workflows": [regen["name"]], "types": ["completed"]}
    }
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    assert "needs" not in job
    steps = job["steps"]

    # Only pinned third-party actions: nothing from the PR's .github/actions.
    for step in steps:
        if "uses" in step:
            assert not step["uses"].startswith("./"), step["name"]
            assert re.search(r"@[0-9a-f]{40}$", step["uses"]), step["name"]
    # No resolver, package manager or repo script runs here.
    runs = "\n".join(step.get("run", "") for step in steps)
    assert not re.search(r"(?<![\w.-])(uv|uvx|pnpm|npm|npx|pip|just|make)\s", runs)
    assert "scripts/" not in runs

    (checkout,) = [step for step in steps if "actions/checkout@" in step.get("uses", "")]
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert checkout["with"]["sparse-checkout"].split() == ["uv.lock", "pnpm-lock.yaml"]
    assert checkout["with"]["sparse-checkout-cone-mode"] is False

    (token,) = [step for step in steps if "create-github-app-token@" in step.get("uses", "")]
    granted = {key for key in token["with"] if key.startswith("permission-")}
    assert granted == {"permission-contents"}
    assert token["with"]["permission-contents"] == "write"
    assert token["with"]["repositories"] == "${{ github.event.repository.name }}"
    # The one step that sees the token commits and pushes with hooks off.
    (push,) = [step for step in steps if "app-token.outputs.token" in str(step.get("env", {}))]
    assert "core.hooksPath=/dev/null" in push["run"]
    assert "git_safe commit" in push["run"] and "git_safe push" in push["run"]

    assert "dependabot-regen-lockfiles" in text
    assert 'ALLOWED = {"uv.lock", "pnpm-lock.yaml"}' in text
