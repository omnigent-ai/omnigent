"""Exercise the trusted CLI against simulated GitHub/PyPI service boundaries."""

from __future__ import annotations

import base64
import copy
import difflib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github/scripts"
HEAD, BASE = "a" * 40, "b" * 40
REPO = "example/project"
PREFIX = f"repos/{REPO}"
POLICY = {"python": {"minor": ["ruff"]}, "cooldown_days": 7}
LOCK = """version = 1
[[package]]
name = "ruff"
version = "0.15.1"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/ruff.whl", hash = "sha256:abc", size = 42 }]
[[package]]
name = "werkzeug"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
"""


@pytest.fixture
def policy_module(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "dependabot_policy", SCRIPTS / "dependabot_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api():
    pull = {
        "number": 123,
        "state": "open",
        "draft": False,
        "changed_files": 1,
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "head": {"sha": HEAD, "ref": "dependabot/uv/ruff", "repo": {"full_name": REPO}},
        "base": {"sha": BASE, "ref": "main"},
        "labels": [],
    }
    routes = {
        f"{PREFIX}/pulls/123": pull,
        f"{PREFIX}/git/ref/heads/main": {"object": {"sha": BASE}},
        f"{PREFIX}/compare/{BASE}...{HEAD}": {"merge_base_commit": {"sha": BASE}},
        f"{PREFIX}/commits/{HEAD}/statuses?per_page=100&page=1": [
            {"id": 1, "context": "Merge Ready", "state": "success"}
        ],
        f"{PREFIX}/pulls/123/files?per_page=100&page=1": [
            {"filename": "uv.lock", "status": "modified"}
        ],
        f"{PREFIX}/pulls/123/reviews?per_page=100&page=1": [],
        f"{PREFIX}/pulls/123/commits?per_page=100&page=1": [],
        f"{PREFIX}/issues/123/timeline?per_page=100&page=1": [],
        f"{PREFIX}/contents/.github/MAINTAINER?ref=main": {
            "content": base64.b64encode(b"maintainer").decode()
        },
    }
    for sha, text in ((BASE, LOCK), (HEAD, LOCK.replace("0.15.1", "0.15.2"))):
        routes[f"{PREFIX}/git/trees/{sha}"] = {
            "tree": [{"path": "uv.lock", "mode": "100644", "sha": sha}],
            "truncated": False,
        }
        routes[f"{PREFIX}/git/blobs/{sha}"] = {
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
        }
    for name in (
        "Security Scan",
        "Maintainer Approval",
        "Trivy CVE Scan (repository)",
        "Trivy CVE Scan (dev-tools)",
        "Pre-commit checks",
        "Pytest (runtime-core)",
        "Integration (openai-agents)",
        "web test",
        "Browser Contract UI",
        "Docker build",
        "Pytest (codex-parity)",
    ):
        routes[name] = {
            "total_count": 1,
            "check_runs": [
                {
                    "id": 1,
                    "app": {"slug": "github-actions"},
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
    workflow_jobs = json.loads((ROOT / ".github/dependabot-automerge.json").read_text())[
        "workflow_test_jobs"
    ]
    for run_id, workflow in enumerate(
        (
            "trivy.yml",
            "ci.yml",
            "lint.yml",
            "integration.yml",
            "e2e.yml",
            "web-tests.yml",
            "e2e-ui.yml",
            "docker-build.yml",
            "windows.yml",
        ),
        start=11,
    ):
        routes[f"{PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1"] = {
            "jobs": [
                {
                    "name": name.replace("*", "runtime-core" if name == "Pytest (*)" else "1/1"),
                    "conclusion": "success",
                }
                for name in workflow_jobs[workflow]
            ]
        }
        routes[
            f"{PREFIX}/actions/workflows/{workflow}/runs?head_sha={HEAD}&event=pull_request&per_page=100"
        ] = {
            "workflow_runs": [
                {
                    "id": run_id,
                    "head_sha": HEAD,
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [
                        {"number": 123, "head": {"sha": HEAD}, "base": {"sha": BASE}}
                    ],
                }
            ]
        }
    routes[f"{PREFIX}/actions/runs/12/jobs?per_page=100&page=1"]["jobs"].extend(
        {"name": name, "conclusion": "success"} for name in ("CI scope", "Pytest (codex-parity)")
    )
    routes[f"{PREFIX}/actions/runs/11/artifacts?per_page=100"] = {
        "total_count": 1,
        "artifacts": [{"name": "trivy-cve-report-repository", "id": 12, "expired": False}],
    }
    return {
        "routes": routes,
        "pypi": {
            "info": {"version": "0.15.2"},
            "urls": [
                {
                    "url": "https://files.pythonhosted.org/ruff.whl",
                    "digests": {"sha256": "abc"},
                    "size": 42,
                    "yanked": False,
                    "upload_time_iso_8601": "2020-01-01T00:00:00Z",
                }
            ],
        },
        "report": {
            "Results": [
                {
                    "Target": "uv.lock",
                    "Type": "uv",
                    "Packages": [{"Name": "ruff", "Version": "0.15.2"}],
                    "Vulnerabilities": [
                        {
                            "PkgName": "werkzeug",
                            "InstalledVersion": "1.0.0",
                            "Severity": "CRITICAL",
                            "VulnerabilityID": "CVE-existing",
                        }
                    ],
                }
            ]
        },
    }


def run_cli(
    tmp_path,
    api,
    *,
    mode="report",
    sweep=False,
    approval=False,
    readiness=False,
    scan=False,
    merge_token="test-merge-token",
):
    (tmp_path / "api.json").write_text(json.dumps(api))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """
import base64, io, json, os, sys, zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
root = Path(os.environ['PILOT_FIXTURES'])
data = json.loads((root / 'api.json').read_text())
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:2] == ['pr', 'list']:
    print(json.dumps(data.get('pulls', [{'number': 123}])))
elif args[:2] == ['pr', 'merge']:
    assert os.environ['GH_TOKEN'] == 'test-merge-token'
    assert args == ['pr', 'merge', '123', '--repo', 'example/project',
                    '--squash', '--match-head-commit', 'a' * 40]
    sys.exit(data.get('merge_exit', 0))
elif args[:2] == ['workflow', 'run']:
    assert os.environ['GH_TOKEN'] == 'test-merge-token'
    assert args == ['workflow', 'run', 'merge-ready.yml', '--repo', 'example/project',
                    '--ref', 'main', '-f', 'pr=123', '-f', 'sha=' + 'a' * 40]
elif args[:2] == ['api', 'repos/example/project/statuses/' + 'a' * 40]:
    assert os.environ['GH_TOKEN'] == 'test-only'
    sys.exit(data.get('status_exit', 0))
elif args[:3] == ['api', '--method', 'PUT']:
    assert os.environ['GH_TOKEN'] == 'test-merge-token'
    assert args == ['api', '--method', 'PUT', 'repos/example/project/pulls/123/update-branch',
                    '-f', 'expected_head_sha=' + 'a' * 40]
    if data.get('update_exit'):
        print(data.get('update_error', 'gh: branch update rejected (HTTP 422)'), file=sys.stderr)
        sys.exit(data['update_exit'])
elif args[:3] == ['api', '--method', 'POST']:
    assert os.environ['GH_TOKEN'] == 'test-merge-token'
    assert args[3].endswith(('/rerun-failed-jobs', '/rerun'))
elif args[0] == 'api':
    assert os.environ['GH_TOKEN'] == 'test-only'
    endpoint = args[1]
    if endpoint.endswith('/pulls/123') and data.get('pull_api_exit'):
        sys.exit(data['pull_api_exit'])
    if '/artifacts/' in endpoint and endpoint.endswith('/zip'):
        count = sum(json.loads(line) == args
                    for line in (root / 'calls.jsonl').read_text().splitlines())
        if count == 1 and 'first_artifact' in data:
            sys.stdout.buffer.write(base64.b64decode(data['first_artifact']))
            sys.exit(0)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            artifact_id = endpoint.rsplit('/', 2)[1]
            report = (data['report'] if artifact_id == '12'
                      else data['artifact_reports'][artifact_id])
            archive.writestr('trivy-results.json', json.dumps(report))
        sys.stdout.buffer.write(buf.getvalue())
    else:
        if '/check-runs?' in endpoint:
            endpoint = parse_qs(urlsplit(endpoint).query)['check_name'][0]
        if '/merge-ready.yml/runs?' in endpoint:
            query = parse_qs(urlsplit(endpoint).query)
            assert query['event'] == ['workflow_dispatch']
            assert query['created'][0].startswith('>=')
            value = data.get('readiness_dispatches', {'total_count': 0, 'workflow_runs': []})
        else:
            value = data['routes'][endpoint]
        if endpoint.endswith('/pulls/123') and 'pull_after' in data:
            count = sum(json.loads(line) == args
                        for line in (root / 'calls.jsonl').read_text().splitlines())
            if count > data.get('pull_after_reads', 1):
                value = data['pull_after']
        if endpoint.endswith('/pulls/123/reviews?per_page=100&page=1') and 'reviews_after' in data:
            count = sum(json.loads(line) == args
                        for line in (root / 'calls.jsonl').read_text().splitlines())
            if count > 1:
                value = data['reviews_after']
        if endpoint.endswith('/git/ref/heads/main') and 'main_after' in data:
            count = sum(json.loads(line) == args
                        for line in (root / 'calls.jsonl').read_text().splitlines())
            if count > 1:
                value = data['main_after']
        print(json.dumps(value))
else:
    raise AssertionError(args)
"""
    )
    gh.chmod(0o755)
    (tmp_path / "sitecustomize.py").write_text("""
import io, json, os, sys, urllib.request
from pathlib import Path
if json.loads((Path(os.environ['PILOT_FIXTURES']) / 'api.json').read_text()).get('parser_missing'):
    class MissingParser:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'yaml':
                raise ModuleNotFoundError('simulated unavailable PyYAML')
    sys.meta_path.insert(0, MissingParser())
def fixture_urlopen(url, timeout):
    url = getattr(url, "full_url", url)
    root = Path(os.environ['PILOT_FIXTURES'])
    (root / 'pypi-called').touch()
    data = json.loads((root / 'api.json').read_text())
    if data.get('pypi_timeout'):
        raise TimeoutError('simulated PyPI timeout')
    if url == 'https://pypi.org/pypi/ruff/0.15.2/json':
        result = data['pypi']
    else:
        result = data['registry'][url]
    return io.BytesIO(json.dumps(result).encode())
urllib.request.urlopen = fixture_urlopen
""")
    args = [
        sys.executable,
        str(SCRIPTS / ("maintainer_approval.py" if approval else "dependabot_automerge.py")),
        "--repository",
        REPO,
    ]
    if not sweep:
        args += ["--pr-number", "123"]
    if not sweep and not approval:
        args += ["--expected-head", HEAD]
    if scan or readiness:
        workflow_name = "merge-ready" if readiness else "security-scan"
        workflow = yaml.safe_load((ROOT / f".github/workflows/{workflow_name}.yml").read_text())
        steps = workflow["jobs"]["evaluate" if readiness else "scan"]["steps"]
        step = next(
            s for s in steps if s.get("id") == ("dependency" if readiness else "dependabot")
        )
        python = tmp_path / "python3"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        args = ["bash", "-e", "-c", step["run"]]
    env = dict(
        os.environ,
        PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        PYTHONPATH=str(tmp_path),
        PILOT_FIXTURES=str(tmp_path),
        GH_TOKEN="test-only",
        DEPENDABOT_MERGE_TOKEN=merge_token,
        DEPENDABOT_AUTOMERGE_MODE=mode,
        GITHUB_OUTPUT=str(tmp_path / "outputs"),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
        REPO=REPO,
        PR="123",
        HEAD_SHA=HEAD,
        EXPECTED_HEAD=HEAD,
        SHA=HEAD,
    )
    if readiness:
        labels = next(s for s in steps if s.get("id") == "labels")
        label_outputs = tmp_path / "label-outputs"
        process = subprocess.run(
            ["bash", "-e", "-c", labels["run"]],
            cwd=ROOT,
            env=dict(env, GITHUB_OUTPUT=str(label_outputs)),
            text=True,
            capture_output=True,
            timeout=30,
        )
        labels_succeeded = process.returncode == 0
        if labels_succeeded:
            context = dict(line.split("=", 1) for line in label_outputs.read_text().splitlines())
            if context["dependency_required"] == "true":
                process = subprocess.run(
                    args, cwd=ROOT, env=env, text=True, capture_output=True, timeout=90
                )
            else:
                process = subprocess.CompletedProcess(
                    args, 0, "Dependency reports not applicable\n", ""
                )
    else:
        process = subprocess.run(
            args, cwd=ROOT, env=env, text=True, capture_output=True, timeout=90
        )
    if readiness and labels_succeeded:
        gate_outputs = tmp_path / "gate-outputs"
        gate = next(s for s in steps if s.get("id") == "gate")
        result = subprocess.run(
            ["bash", "-e", "-c", gate["run"]],
            cwd=ROOT,
            env=dict(
                env,
                EVAL=api.get("ci_eval", "success"),
                FAILED="CI failure",
                DEPENDENCY_EVAL=("success" if process.returncode == 0 else "failure")
                if context["dependency_required"] == "true"
                else "skipped",
                DEPENDENCY_REQUIRED=context["dependency_required"],
                POST_RED=api.get("post_red", "false"),
                GITHUB_OUTPUT=str(gate_outputs),
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        outputs = dict(
            line.split("=", 1) for line in gate_outputs.read_text().splitlines() if "=" in line
        )
        post = next(s for s in steps if s["name"] == "Post Merge Ready status on PR head SHA")
        if outputs["post_status"] == "true":
            subprocess.run(
                ["bash", "-e", "-c", post["run"]],
                cwd=ROOT,
                env=dict(env, STATE=outputs["state"], DESC=outputs["short_desc"]),
                text=True,
                capture_output=True,
                check=True,
            )
        process = subprocess.CompletedProcess(
            process.args, process.returncode, process.stdout + result.stdout, process.stderr
        )
    calls_file = tmp_path / "calls.jsonl"
    calls = (
        [json.loads(line) for line in calls_file.read_text().splitlines()]
        if calls_file.exists()
        else []
    )
    return process, calls


def test_report_mode_is_read_only_and_existing_advisories_do_not_disqualify(tmp_path, api):
    process, calls = run_cli(tmp_path, api, sweep=True)
    assert process.returncode == 0, process.stderr
    assert "eligible (ruff==0.15.2)" in process.stdout
    assert all(call[:2] != ["pr", "merge"] for call in calls)
    assert "Mode: `report`" in (tmp_path / "summary").read_text()


def test_merge_uses_exact_head_after_completed_reports(tmp_path, api):
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert calls[-1] == [
        "pr",
        "merge",
        "123",
        "--repo",
        REPO,
        "--squash",
        "--match-head-commit",
        HEAD,
    ]
    assert "Merged after policy" in process.stdout


@pytest.mark.parametrize("dependabot", ["true", "false", ""])
def test_security_audit_does_not_gate_dependabot_ci_on_baseline(tmp_path, dependabot):
    workflow = yaml.safe_load((ROOT / ".github/workflows/security-scan.yml").read_text())
    audit = next(
        s for s in workflow["jobs"]["scan"]["steps"] if s["name"] == "OSV advisory scan (uv.lock)"
    )
    assert audit["if"] == "${{ steps.gate.outputs.scan == 'true' }}"
    identity = audit["env"]["DEPENDABOT_PR"]
    assert "user.login == 'dependabot[bot]'" in identity
    assert "user.type == 'Bot'" in identity
    assert "head.repo.full_name == github.repository" in identity
    (tmp_path / "changed.txt").write_text("uv.lock\n")
    for name, command in (
        ("uv", "printf 'ruff==0.15.2\\n'"),
        ("uvx", 'echo "$*" >> "$GITHUB_WORKSPACE/audit-calls"'),
    ):
        stub = tmp_path / name
        stub.write_text(f"#!/bin/sh\n{command}\n")
        stub.chmod(0o755)
    # Keep the production shell's temporary requirements files inside this scenario.
    script = audit["run"].replace("/tmp/uv-req", str(tmp_path / "uv-req"))
    process = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env=dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            GITHUB_WORKSPACE=str(tmp_path),
            DEPENDABOT_PR=dependabot,
        ),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    audit_calls = tmp_path / "audit-calls"
    if dependabot == "true":
        assert not audit_calls.exists()
        assert "Trivy findings are checked by the merge worker or a maintainer" in process.stdout
    else:
        assert audit_calls.read_text().splitlines() == [
            f"pip-audit --requirement {tmp_path}/uv-req.txt --no-deps",
            f"pip-audit --requirement {tmp_path}/uv-req-ag.txt --no-deps",
        ]


def test_merge_mode_never_falls_back_to_workflow_token(tmp_path, api):
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True, merge_token="")
    assert process.returncode != 0
    assert "requires a GitHub App token" in process.stderr
    assert calls == []


@pytest.mark.parametrize("change", ["head", "main", "target", "hold"])
@pytest.mark.parametrize("entry", ["merge", "approval", "readiness"])
def test_candidate_changed_during_scanning_is_not_merged(tmp_path, api, change, entry):
    api["pull_after"] = copy.deepcopy(api["routes"][f"{PREFIX}/pulls/123"])
    api["pull_after_reads"] = {"merge": 1, "approval": 2, "readiness": 3}[entry]
    if change == "head":
        api["pull_after"]["head"]["sha"] = "c" * 40
    elif change == "main":
        api["main_after"] = {"object": {"sha": "c" * 40}}
    elif change == "target":
        api["pull_after"]["base"]["ref"] = "release"
    else:
        api["pull_after"]["labels"] = [{"name": "hold-automerge"}]
    process, calls = run_cli(
        tmp_path,
        api,
        mode="merge",
        sweep=entry == "merge",
        approval=entry == "approval",
        readiness=entry == "readiness",
    )
    assert process.returncode == (0 if entry == "merge" else 1), process.stderr
    assert (
        "human review or retry required" if entry == "merge" else "requires human review"
    ) in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize(
    "mode,fault,expected",
    [
        ("report", None, 1),
        ("merge", None, 0),
        ("merge", "hold", 1),
        ("merge", "ghost", 1),
        ("merge", "timestamp", 1),
        ("merge", "timeout", 1),
    ],
)
def test_real_approval_entry_point_preserves_review_unless_pilot_enabled(
    tmp_path, api, mode, fault, expected
):
    # The approval gate only checks eligibility; reports belong to Merge Ready.
    api["routes"]["Maintainer Approval"]["check_runs"][0]["conclusion"] = "failure"
    if fault == "hold":
        api["routes"][f"{PREFIX}/pulls/123"]["labels"] = [{"name": "hold-automerge"}]
    elif fault == "ghost":
        api["routes"][f"{PREFIX}/pulls/123"]["user"] = None
    elif fault == "timestamp":
        api["pypi"]["urls"][0]["upload_time_iso_8601"] = "2020-01-01T00:00:00"
    elif fault == "timeout":
        api["pypi_timeout"] = True
    process, calls = run_cli(tmp_path, api, mode=mode, approval=True)
    assert process.returncode == expected, process.stdout + process.stderr
    expected_message = {
        "hold": "A hold or scan-waiver label is present",
        "timestamp": "Registry timestamp has no timezone",
        "timeout": "simulated PyPI timeout",
    }.get(fault, "awaiting approval from a maintainer" if expected else "policy satisfied")
    assert expected_message in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


def test_human_approval_does_not_depend_on_pypi(tmp_path, api):
    api["routes"][f"{PREFIX}/pulls/123/reviews?per_page=100&page=1"] = [
        {"user": {"login": "maintainer"}, "state": "APPROVED", "commit_id": HEAD}
    ]
    api["pypi_timeout"] = True
    process, _calls = run_cli(tmp_path, api, mode="merge", approval=True)
    assert process.returncode == 0, process.stdout + process.stderr
    assert "Current head approved" in process.stdout
    assert not (tmp_path / "pypi-called").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "author",
        "fork",
        "draft",
        "base",
        "hold",
        "waiver",
        "manifest",
        "symlink",
        "head",
        "review",
        "hash",
        "size",
        "null_files",
        "yanked",
        "young",
    ],
)
def test_ineligible_changes_keep_human_review(tmp_path, api, fault):
    pull = api["routes"][f"{PREFIX}/pulls/123"]
    if fault == "author":
        pull["user"]["login"] = "contributor"
    elif fault == "fork":
        pull["head"]["repo"]["full_name"] = "contributor/project"
    elif fault == "draft":
        pull["draft"] = True
    elif fault == "base":
        pull["base"]["ref"] = "release/v1"
    elif fault in {"hold", "waiver"}:
        pull["labels"] = [{"name": "hold-automerge" if fault == "hold" else "skip-security-scan"}]
    elif fault == "manifest":
        api["routes"][f"{PREFIX}/pulls/123/files?per_page=100&page=1"][0]["filename"] = (
            "pyproject.toml"
        )
    elif fault == "symlink":
        api["routes"][f"{PREFIX}/git/trees/{HEAD}"]["tree"][0]["mode"] = "120000"
    elif fault == "head":
        pull["head"]["sha"] = "c" * 40
    elif fault == "review":
        api["routes"][f"{PREFIX}/pulls/123/reviews?per_page=100&page=1"] = [
            {"user": {"login": "maintainer"}, "state": "CHANGES_REQUESTED"}
        ]
    elif fault == "hash":
        api["pypi"]["urls"][0]["digests"]["sha256"] = "different"
    elif fault == "size":
        api["pypi"]["urls"][0]["size"] += 1
    elif fault == "null_files":
        api["routes"][f"{PREFIX}/pulls/123/files?per_page=100&page=1"] = None
    elif fault == "yanked":
        api["pypi"]["urls"][0]["yanked"] = True
    elif fault == "young":
        api["pypi"]["urls"][0]["upload_time_iso_8601"] = "2100-01-01T00:00:00Z"
    process, _calls = run_cli(tmp_path, api, mode="merge")
    assert process.returncode == 0, process.stderr
    assert (tmp_path / "outputs").read_text() == "eligible=false\n"
    assert "Human review required" in process.stdout


@pytest.mark.parametrize(
    "entry,fault",
    [
        (entry, fault)
        for entry in ("merge", "approval", "readiness")
        for fault in (
            "failed_scan",
            "pending_scan",
            "missing_scan",
            "newer_scan",
            "missing_inventory",
            "wrong_inventory",
            "severe",
            "pending_ci",
            "old_coverage",
        )
    ]
    + [("merge", "merge_blocked")],
)
def test_no_merge_without_reports_and_required_checks(tmp_path, api, entry, fault):
    scan = api["routes"]["Trivy CVE Scan (repository)"]
    if fault == "failed_scan":
        scan["check_runs"][0]["conclusion"] = "failure"
    elif fault == "pending_scan":
        scan["check_runs"][0]["status"] = "in_progress"
    elif fault == "missing_scan":
        scan["check_runs"] = []
    elif fault == "newer_scan":
        scan["check_runs"].append(
            dict(scan["check_runs"][0], id=2, status="queued", conclusion=None)
        )
    elif fault == "missing_inventory":
        api["report"]["Results"][0]["Packages"] = []
    elif fault == "wrong_inventory":
        api["report"]["Results"][0]["Target"] = "another/uv.lock"
    elif fault == "severe":
        api["report"]["Results"][0]["Vulnerabilities"].append(
            {
                "PkgName": "ruff",
                "InstalledVersion": "0.15.2",
                "Severity": "HIGH",
                "VulnerabilityID": "CVE-new",
            }
        )
    elif fault == "merge_blocked":
        api["merge_exit"] = 1
    elif fault == "pending_ci":
        api["routes"][
            f"{PREFIX}/actions/workflows/ci.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
        ]["workflow_runs"][0]["status"] = "in_progress"
    elif fault == "old_coverage":
        jobs = api["routes"][f"{PREFIX}/actions/runs/12/jobs?per_page=100&page=1"]["jobs"]
        jobs[:] = [job for job in jobs if job["name"] != "CI coverage scope"]
    process, calls = run_cli(
        tmp_path,
        api,
        mode="merge",
        sweep=entry == "merge",
        approval=entry == "approval",
        readiness=entry == "readiness",
    )
    assert process.returncode == (1 if entry == "readiness" else 0), process.stderr
    if entry == "approval":
        assert "policy satisfied" in process.stdout
        assert not any("check-runs" in str(call) or "/commits/" in str(call) for call in calls)
        assert "state=pending" in calls[-1]
        return
    if entry == "readiness":
        assert "state=failure" in calls[-1]
    assert "Merged after policy" not in process.stdout
    assert (
        "human review or retry required" if entry == "merge" else "requires human review"
    ) in process.stdout
    if fault != "merge_blocked":
        assert not any(call[:2] == ["pr", "merge"] for call in calls)


def test_changed_package_with_medium_advisory_can_merge(tmp_path, api):
    api["report"]["Results"][0]["Vulnerabilities"].append(
        {
            "PkgName": "ruff",
            "InstalledVersion": "0.15.2",
            "Severity": "MEDIUM",
            "VulnerabilityID": "CVE-remaining",
        }
    )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert calls[-1][:2] == ["pr", "merge"]


@pytest.mark.parametrize("fault", ["zip", "deflate", "null_files"])
def test_bad_candidate_does_not_stop_later_merges(tmp_path, api, fault):
    api["pulls"] = [{"number": 122}, {"number": 123}]
    for endpoint, value in list(api["routes"].items()):
        if "/pulls/123" in endpoint:
            api["routes"][endpoint.replace("/pulls/123", "/pulls/122")] = copy.deepcopy(value)
        if isinstance(value, dict):
            for run in value.get("workflow_runs", []):
                run["pull_requests"].append(
                    {"number": 122, "head": {"sha": HEAD}, "base": {"sha": BASE}}
                )
    if fault == "null_files":
        api["routes"][f"{PREFIX}/pulls/122/files?per_page=100&page=1"] = None
    else:
        data = b"not a zip file"
        if fault == "deflate":
            buffer = io.BytesIO()
            filename = "trivy-results.json"
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(filename, json.dumps(api["report"]))
            damaged = bytearray(buffer.getvalue())
            damaged[30 + len(filename)] = 7  # Invalid DEFLATE block type.
            data = bytes(damaged)
        api["first_artifact"] = base64.b64encode(data).decode()
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stdout + process.stderr
    summary = (tmp_path / "summary").read_text()
    assert "#122: human review or retry required" in summary
    assert "#123: eligible" in summary
    assert "Merged after policy" in summary
    assert [call[2] for call in calls if call[:2] == ["pr", "merge"]] == ["123"]


@pytest.mark.parametrize(
    "change",
    [
        "minor",
        "prerelease",
        "downgrade",
        "runtime",
        "source",
        "dependencies",
        "new_package",
        "settings",
        "conditional",
    ],
)
def test_lock_policy_rejects_changes_outside_patch_scope(policy_module, change):
    before = policy_module.tomllib.loads(LOCK)
    after = copy.deepcopy(before)
    after["package"][0]["version"] = "0.15.2"
    if change == "minor":
        after["package"][0]["version"] = "0.16.0"
    elif change == "prerelease":
        after["package"][0]["version"] = "0.15.2rc1"
    elif change == "downgrade":
        after["package"][0]["version"] = "0.15.0"
    elif change == "runtime":
        after["package"][1]["version"] = "1.0.1"
    elif change == "source":
        after["package"][0]["source"] = {"registry": "https://other.example/simple"}
    elif change == "dependencies":
        after["package"][0]["dependencies"] = [{"name": "werkzeug"}]
    elif change == "new_package":
        after["package"].append({"name": "new", "version": "1"})
    elif change == "settings":
        after["revision"] = 2
    elif change == "conditional":
        after["package"].append(dict(after["package"][0], version="0.15.3"))
    with pytest.raises(policy_module.NeedsReview):
        policy_module.lock_changes(before, after, POLICY)


def test_age_uses_registry_timestamp_and_accepts_exact_boundary(policy_module, api):
    package = policy_module.tomllib.loads(LOCK.replace("0.15.1", "0.15.2"))["package"][0]
    package["wheels"][0]["upload-time"] = "1900-01-01T00:00:00Z"
    with pytest.raises(policy_module.NeedsReview, match="cooldown"):
        policy_module.validate_release(package, api["pypi"], 7, datetime(2020, 1, 7, tzinfo=UTC))
    policy_module.validate_release(package, api["pypi"], 7, datetime(2020, 1, 8, tzinfo=UTC))


def set_git_files(api, commit, files):
    """Serve Git trees and blobs for one revision without changing the PR inventory."""
    trees = {commit: {}}
    for path, contents in files.items():
        parts = path.split("/")
        tree_id = commit
        for part in parts[:-1]:
            child = tree_id + "/" + part
            trees[tree_id][part] = {"path": part, "mode": "040000", "sha": child}
            trees.setdefault(child, {})
            tree_id = child
        blob_id = commit + "/" + path
        trees[tree_id][parts[-1]] = {"path": parts[-1], "mode": "100644", "sha": blob_id}
        api["routes"][f"{PREFIX}/git/blobs/{blob_id}"] = {
            "encoding": "base64",
            "content": base64.b64encode(contents.encode()).decode(),
        }
    for sha, tree in trees.items():
        api["routes"][f"{PREFIX}/git/trees/{sha}"] = {"tree": list(tree.values())}


def set_changed_files(api, files, *, number=123, head=HEAD):
    """Serve real Git tree/blob shapes for each base/head fixture file."""
    api["files"] = files
    changed = [path for path, (old, new) in files.items() if old != new]
    api["routes"][f"{PREFIX}/pulls/{number}"]["changed_files"] = len(changed)
    api["routes"][f"{PREFIX}/pulls/{number}/files?per_page=100&page=1"] = [
        {"filename": path, "status": "modified"} for path in changed
    ]
    for index, commit in enumerate((BASE, head)):
        set_git_files(api, commit, {path: contents[index] for path, contents in files.items()})


@pytest.fixture
def expanded_update(api, request):
    ecosystem = request.param
    api["registry"] = {}
    if ecosystem == "python":
        old, new = (
            LOCK.replace("ruff", "cachetools"),
            LOCK.replace("ruff", "cachetools").replace("0.15.1", "0.15.2"),
        )
        set_changed_files(api, {"uv.lock": (old, new)})
        api["registry"]["https://pypi.org/pypi/cachetools/0.15.2/json"] = copy.deepcopy(
            api["pypi"]
        )
        api["registry"]["https://pypi.org/pypi/cachetools/0.15.2/json"]["urls"][0]["url"] = (
            "https://files.pythonhosted.org/cachetools.whl"
        )
        name, pin, target, scanner = "cachetools", "0.15.2", "uv.lock", "uv"
    elif ecosystem == "npm":
        lock = {
            "lockfileVersion": "9.0",
            "settings": {"autoInstallPeers": True},
            "importers": {
                "web": {"dependencies": {"clsx": {"specifier": "^2.1.0", "version": "2.1.0"}}}
            },
            "packages": {"clsx@2.1.0": {"resolution": {"integrity": "sha512-old"}}},
            "snapshots": {"clsx@2.1.0": {}},
        }
        old = yaml.safe_dump(lock)
        new = old.replace("2.1.0", "2.1.1").replace("sha512-old", "sha512-new")
        manifest = json.dumps({"dependencies": {"clsx": "^2.1.0"}, "scripts": {"test": "vitest"}})
        set_changed_files(
            api,
            {
                "pnpm-lock.yaml": (old, new),
                "web/package.json": (manifest, manifest.replace("2.1.0", "2.1.1")),
                "pnpm-workspace.yaml": ("packages: [web]\n", "packages: [web]\n"),
            },
        )
        api["registry"]["https://registry.npmjs.org/clsx"] = {
            "versions": {
                "2.1.1": {
                    "name": "clsx",
                    "version": "2.1.1",
                    "dist": {
                        "integrity": "sha512-new",
                        "tarball": "https://registry.npmjs.org/clsx/-/clsx-2.1.1.tgz",
                    },
                }
            },
            "time": {"2.1.1": "2020-01-01T00:00:00Z"},
        }
        name, pin, target, scanner = "clsx", "2.1.1", "pnpm-lock.yaml", "pnpm"
    elif ecosystem == "cargo":
        old = (
            'version = 4\n[[package]]\nname = "anyhow"\nversion = "1.0.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            'checksum = "old"\n'
        )
        set_changed_files(
            api,
            {
                "tests/codex_parity/sidecar/Cargo.lock": (
                    old,
                    old.replace("1.0.0", "1.0.1").replace('"old"', '"new"'),
                )
            },
        )
        api["registry"]["https://crates.io/api/v1/crates/anyhow/1.0.1"] = {
            "version": {
                "num": "1.0.1",
                "checksum": "new",
                "yanked": False,
                "created_at": "2020-01-01T00:00:00Z",
            }
        }
        name, pin, target, scanner = (
            "anyhow",
            "1.0.1",
            "tests/codex_parity/sidecar/Cargo.lock",
            "cargo",
        )
    elif ecosystem == "github-actions":
        old = (
            "name: CI\non: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@" + "1" * 40 + " # v4.1.0\n"
        )
        new = old.replace("1" * 40, "2" * 40).replace("v4.1.0", "v4.2.0")
        set_changed_files(api, {".github/workflows/ci.yml": (old, new)})
        api["routes"]["repos/actions/checkout/tags?per_page=100&page=1"] = [
            {"name": tag, "commit": {"sha": sha * 40}}
            for tag, sha in [("v4.1.0", "1"), ("v4.2.0", "2")]
        ]
        for tag in ["v4.1.0", "v4.2.0"]:
            api["routes"][f"repos/actions/checkout/releases/tags/{tag}"] = {
                "draft": False,
                "prerelease": False,
                "published_at": "2020-01-01T00:00:00Z",
            }
        api["routes"][
            "advisories?ecosystem=actions&affects=actions%2Fcheckout%404.2.0&per_page=100&page=1"
        ] = []
        name, pin, target, scanner = "actions/checkout", "4.2.0", "", ""
    else:
        assert ecosystem == "pre-commit"
        old = (
            "repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
            "    rev: v6.0.0\n    hooks:\n      - id: check-yaml\n"
        )
        set_changed_files(api, {".pre-commit-config.yaml": (old, old.replace("v6.0.0", "v6.1.0"))})
        api["routes"]["repos/pre-commit/pre-commit-hooks/releases/tags/v6.1.0"] = {
            "draft": False,
            "prerelease": False,
            "published_at": "2020-01-01T00:00:00Z",
        }
        api["routes"][
            "advisories?ecosystem=pip&affects=pre-commit-hooks%406.1.0&per_page=100&page=1"
        ] = []
        name, pin, target, scanner = (
            "https://github.com/pre-commit/pre-commit-hooks",
            "v6.1.0",
            "",
            "",
        )
    api["report"]["Results"][0].update(
        Target=target, Type=scanner, Packages=[{"Name": name, "Version": pin}]
    )
    return ecosystem, api


@pytest.mark.parametrize(
    "expanded_update", ["python", "npm", "cargo", "github-actions", "pre-commit"], indirect=True
)
@pytest.mark.parametrize("entry", ["merge", "approval", "readiness", "scan"])
def test_expanded_updates_use_production_entry_points(tmp_path, expanded_update, entry):
    ecosystem, api = expanded_update
    process, calls = run_cli(
        tmp_path,
        api,
        mode="merge",
        sweep=entry == "merge",
        approval=entry == "approval",
        readiness=entry == "readiness",
        scan=entry == "scan",
    )
    assert process.returncode == 0, process.stdout + process.stderr
    if entry == "merge":
        assert calls[-1][:2] == ["pr", "merge"], process.stdout
    elif entry in {"approval", "readiness"}:
        assert "policy satisfied" in process.stdout
        if entry == "readiness":
            assert "state=success" in calls[-1]
    else:
        output = (tmp_path / "outputs").read_text()
        assert "eligible=" not in output
        assert ("workflow_pins=true" in output) == (ecosystem == "github-actions")
        if ecosystem == "github-actions":
            changed = tmp_path / "changed.txt"
            changed.write_text(".github/workflows/ci.yml\n")
            for authorized, expected in [("true", 0), ("", 1)]:
                guard = subprocess.run(
                    ["bash", str(SCRIPTS / "security-scan/sensitive-paths.sh")],
                    env=dict(
                        os.environ, CHANGED_FILES=str(changed), DEPENDABOT_WORKFLOW_PINS=authorized
                    ),
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                assert guard.returncode == expected, guard.stdout + guard.stderr
            workflow = yaml.safe_load((ROOT / ".github/workflows/security-scan.yml").read_text())
            sensitive = next(
                s for s in workflow["jobs"]["scan"]["steps"] if s["name"] == "Sensitive-path guard"
            )
            assert (
                sensitive["env"]["DEPENDABOT_WORKFLOW_PINS"]
                == "${{ steps.dependabot.outputs.workflow_pins }}"
            )
            path = ".github/workflows/ci.yml"
            old, new = api["files"][path]
            checked_out = tmp_path / path
            checked_out.parent.mkdir(parents=True)
            checked_out.write_text(new)
            diff = tmp_path / "pr.diff"
            diff.write_text(
                f"diff --git a/{path} b/{path}\n"
                + "".join(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                    )
                )
            )
            for script in ("secret-scan.py", "exfil-scan.py", "lint-workflow-misuse.py"):
                scan = subprocess.run(
                    [sys.executable, str(SCRIPTS / "security-scan" / script)],
                    cwd=tmp_path,
                    env=dict(os.environ, DIFF_FILE=str(diff), CHANGED_FILES=str(changed)),
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                assert scan.returncode == 0, scan.stdout + scan.stderr
    if entry != "merge":
        assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize(
    "expanded_update", ["npm", "cargo", "github-actions", "pre-commit"], indirect=True
)
def test_expanded_updates_require_their_actual_jobs(tmp_path, expanded_update):
    ecosystem, api = expanded_update
    name = {
        "npm": "web test",
        "cargo": "Pytest (codex-parity)",
        "github-actions": "Pre-commit checks",
        "pre-commit": "Pre-commit checks",
    }[ecosystem]
    api["routes"][name]["check_runs"][0]["conclusion"] = "skipped"
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert "human review or retry required" in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize("expanded_update", ["npm", "cargo", "pre-commit"], indirect=True)
@pytest.mark.parametrize("result", ["success", "failure", "skipped", "pending", "missing"])
def test_scoped_dependency_ci_requires_executed_consumer_jobs(tmp_path, expanded_update, result):
    ecosystem, api = expanded_update
    ci_jobs = api["routes"][f"{PREFIX}/actions/runs/12/jobs?per_page=100&page=1"]["jobs"]
    for job in ci_jobs:
        if job["name"] == "Pytest (runtime-core)":
            job["conclusion"] = "skipped"
    if ecosystem == "cargo":
        jobs = [
            {"name": f"E2E UI Tests (shard {index}/2)", "conclusion": "success"}
            for index in range(2)
        ]
        api["routes"][f"{PREFIX}/actions/runs/17/jobs?per_page=100&page=1"]["jobs"] = jobs
        api["routes"]["Browser Contract UI"]["check_runs"][0]["conclusion"] = "skipped"
        jobs.extend(
            {"name": name, "conclusion": "success"}
            for name in ("UI coverage scope", "E2E UI result")
        )
        required = jobs[0]
    else:
        jobs = ci_jobs
        required = next(job for job in jobs if job["name"] == "CI coverage scope")
    if result == "missing":
        jobs.remove(required)
        if ecosystem == "cargo":
            jobs.clear()
    else:
        required["conclusion"] = None if result == "pending" else result
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (result == "success"), (
        process.stdout
    )


@pytest.mark.parametrize("expanded_update", ["npm", "pre-commit"], indirect=True)
def test_pre_selection_heads_cannot_use_old_smoke_ci(tmp_path, expanded_update):
    _ecosystem, api = expanded_update
    route = api["routes"][f"{PREFIX}/actions/runs/12/jobs?per_page=100&page=1"]
    route["jobs"] = [job for job in route["jobs"] if job["name"] != "CI coverage scope"]
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert not any(call[:2] == ["pr", "merge"] for call in calls), process.stdout


def test_lockfile_guards_are_in_the_required_lint_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    job = next(job for job in workflow["jobs"].values() if job.get("name") == "Pre-commit checks")
    guard = next(
        step
        for step in job["steps"]
        if step.get("name") == "Check locked dependency regression guards"
    )
    assert "tests/e2e/test_lockfile_vulnerable_pins.py" in guard["run"]
    assert "-p tests._helpers.required_tests" in guard["run"]
    assert "if" not in guard


@pytest.fixture
def evaluate_fixture(policy_module, expanded_update, monkeypatch):
    _ecosystem, api = expanded_update
    monkeypatch.setattr(policy_module, "registry_json", lambda url: api["registry"][url])
    monkeypatch.setattr(policy_module, "gh_json", lambda args: api["routes"][args[1]])
    monkeypatch.setattr(
        policy_module,
        "paginated",
        lambda endpoint: api["routes"][endpoint + "?per_page=100&page=1"],
    )
    policy = json.loads((ROOT / ".github/dependabot-automerge.json").read_text())

    def evaluate():
        files = api["files"]
        changed = {path for path, (old, new) in files.items() if old != new}
        return policy_module.evaluate_changes(files.__getitem__, changed, policy)

    return api, evaluate


@pytest.mark.parametrize(
    "old,new,minor,allowed",
    [
        ("1.2.3", "1.2.4", False, True),
        ("1.2.3", "1.3.0", True, True),
        ("0.2.3", "0.3.0", True, False),
        ("1.2.3", "2.0.0", True, False),
        ("1.2.3", "1.2.4rc1", True, False),
        ("1.2.3", "1.2.2", True, False),
        ("1.2.3", "1.2.3.0", True, False),
        ("1.2.3", "1.2.3.1", True, True),
        ("1!2.0", "1!2.1", True, False),
        ("1.2.3", "1.2.4+local", True, False),
    ],
)
def test_update_policy_respects_version_boundaries(policy_module, old, new, minor, allowed):
    if allowed:
        policy_module.upgrade(old, new, minor)
    else:
        with pytest.raises(policy_module.NeedsReview):
            policy_module.upgrade(old, new, minor)


@pytest.mark.parametrize("expanded_update", ["npm", "cargo"], indirect=True)
@pytest.mark.parametrize("fault", [None, "mismatch", "high"])
def test_exact_manifest_pins_use_production_worker(tmp_path, expanded_update, fault):
    ecosystem, api = expanded_update
    if ecosystem == "npm":
        for path in ("web/package.json", "pnpm-lock.yaml"):
            api["files"][path] = tuple(
                text.replace("^2.1.", "2.1.") for text in api["files"][path]
            )
        if fault == "mismatch":
            for path in ("web/package.json", "pnpm-lock.yaml"):
                old, new = api["files"][path]
                api["files"][path] = (
                    old,
                    new.replace("2.1.1", "2.1.2")
                    if path.endswith("json")
                    else new.replace("specifier: 2.1.1", "specifier: 2.1.2"),
                )
        set_changed_files(api, dict(api["files"]))
    else:
        manifest = '[dependencies]\nanyhow = "=1.0.0"\n'
        files = dict(api["files"])
        files["tests/codex_parity/sidecar/Cargo.toml"] = (
            manifest,
            manifest.replace("1.0.0", "1.0.2" if fault == "mismatch" else "1.0.1"),
        )
        set_changed_files(api, files)
    if fault == "high":
        report = api["report"]["Results"][0]
        package = report["Packages"][0]
        report["Vulnerabilities"].append(
            {
                "PkgName": package["Name"],
                "InstalledVersion": package["Version"],
                "Severity": "HIGH",
                "VulnerabilityID": "pin-advisory",
            }
        )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) is (fault is None), process.stdout


@pytest.mark.parametrize("expanded_update", ["npm"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "scripts",
        "pin",
        "source",
        "integrity",
        "young",
        "override",
        "shared",
        "graph",
        "duplicate_key",
    ],
)
def test_web_updates_preserve_security_and_compatibility(evaluate_fixture, policy_module, fault):
    api, evaluate = evaluate_fixture
    assert evaluate()["ecosystem"] == "npm"
    files = api["files"]
    if fault == "scripts":
        old, new = files["web/package.json"]
        files["web/package.json"] = (old, new.replace("vitest", "curl attacker"))
    elif fault == "pin":
        for path in ("web/package.json", "pnpm-lock.yaml"):
            old, new = files[path]
            files[path] = (old, new.replace("^2.1.", "2.1."))
    elif fault in {"integrity", "source", "young"}:
        metadata = api["registry"]["https://registry.npmjs.org/clsx"]
        if fault == "young":
            metadata["time"]["2.1.1"] = "2100-01-01T00:00:00Z"
        else:
            metadata["versions"]["2.1.1"]["dist"][
                "integrity" if fault == "integrity" else "tarball"
            ] = "untrusted"
    elif fault == "override":
        text = "packages: [web]\noverrides: {clsx: '~2.0.0'}\n"
        files["pnpm-workspace.yaml"] = (text, text)
        files["pnpm-lock.yaml"] = tuple(
            yaml.safe_dump({**yaml.safe_load(lock), "overrides": {"clsx": "~2.0.0"}})
            for lock in files["pnpm-lock.yaml"]
        )
    elif fault == "shared":
        locks = [yaml.safe_load(text) for text in files["pnpm-lock.yaml"]]
        for lock in locks:
            lock["importers"]["uncovered/workspace"] = copy.deepcopy(lock["importers"]["web"])
        files["pnpm-lock.yaml"] = tuple(yaml.safe_dump(lock) for lock in locks)
    elif fault == "graph":
        old, new = files["pnpm-lock.yaml"]
        lock = yaml.safe_load(new)
        lock["snapshots"]["clsx@2.1.1"] = {"dependencies": {"unexpected": "1.0.0"}}
        files["pnpm-lock.yaml"] = (old, yaml.safe_dump(lock))
    else:
        old, new = files["pnpm-lock.yaml"]
        files["pnpm-lock.yaml"] = (old, new + "\nimporters: {}\n")
    reason = "resolution violates compatibility override" if fault == "override" else None
    with pytest.raises(policy_module.NeedsReview, match=reason):
        evaluate()


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize(
    "fault", ["run", "permission", "unknown_action", "untested", "unreleased", "young"]
)
def test_action_updates_only_change_tested_release_pins(evaluate_fixture, policy_module, fault):
    api, evaluate = evaluate_fixture
    assert evaluate()["ecosystem"] == "github-actions"
    path = ".github/workflows/ci.yml"
    old, new = api["files"][path]
    if fault in {"run", "permission"}:
        new += "      - run: curl attacker\n" if fault == "run" else "permissions: write-all\n"
    elif fault == "unknown_action":
        new = new.replace("actions/checkout", "actions/create-github-app-token")
    elif fault == "untested":
        api["files"] = {".github/workflows/release.yml": (old, new)}
    elif fault == "unreleased":
        new = new.replace("2" * 40, "3" * 40)
    else:
        api["routes"]["repos/actions/checkout/releases/tags/v4.2.0"]["published_at"] = (
            "2100-01-01T00:00:00Z"
        )
    if fault != "untested":
        api["files"][path] = (old, new)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("expanded_update", ["pre-commit", "cargo"], indirect=True)
def test_hooks_and_rust_preserve_commands_and_sources(evaluate_fixture, policy_module):
    api, evaluate = evaluate_fixture
    ecosystem = evaluate()["ecosystem"]
    (path,) = api["files"]
    old, new = api["files"][path]
    new += (
        "    entry: curl attacker\n"
        if ecosystem == "pre-commit"
        else 'dependencies = ["unexpected"]\n'
    )
    api["files"][path] = (old, new)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("expanded_update", ["npm", "cargo"], indirect=True)
def test_expanded_advisories_block_changed_versions(tmp_path, expanded_update):
    _ecosystem, api = expanded_update
    package = api["report"]["Results"][0]["Packages"][0]
    api["report"]["Results"][0]["Vulnerabilities"].append(
        {
            "PkgName": package["Name"],
            "InstalledVersion": package["Version"],
            "Severity": "HIGH",
            "VulnerabilityID": "CVE-new",
        }
    )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert "CVE-new" in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize("expanded_update", ["github-actions", "pre-commit"], indirect=True)
def test_action_and_hook_advisories_block_unattended_merge(tmp_path, expanded_update):
    _ecosystem, api = expanded_update
    (endpoint,) = [key for key in api["routes"] if key.startswith("advisories?")]
    api["routes"][endpoint] = [{"ghsa_id": "GHSA-test", "severity": "high", "withdrawn_at": None}]
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert "GHSA-test" in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize("entry", ["merge", "readiness"])
@pytest.mark.parametrize("fresh_ci", [True, False])
def test_live_main_ref_controls_ci_freshness_despite_stale_pr_base(tmp_path, api, entry, fresh_ci):
    tip = "c" * 40
    api["routes"][f"{PREFIX}/git/ref/heads/main"]["object"]["sha"] = tip
    api["routes"][f"{PREFIX}/compare/{tip}...{HEAD}"] = {"merge_base_commit": {"sha": BASE}}
    if fresh_ci:
        for response in api["routes"].values():
            for run in response.get("workflow_runs", []) if isinstance(response, dict) else []:
                run["pull_requests"][0]["base"]["sha"] = tip
    process, calls = run_cli(
        tmp_path,
        api,
        mode="merge",
        sweep=entry == "merge",
        approval=entry == "approval",
        readiness=entry == "readiness",
    )
    assert process.returncode == (1 if entry == "readiness" and not fresh_ci else 0), (
        process.stderr
    )
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (entry == "merge" and fresh_ci)
    if not fresh_ci:
        assert "CI did not test the current PR head and base" in process.stdout
    assert ["api", f"{PREFIX}/compare/{tip}...{HEAD}"] in calls
    assert ["api", f"{PREFIX}/git/trees/{tip}"] not in calls
    assert ["api", f"{PREFIX}/git/trees/{BASE}"] in calls


@pytest.mark.parametrize("entry", ["merge", "readiness"])
@pytest.mark.parametrize("fault", ["base", "head", "number", "missing"])
def test_required_workflow_must_test_current_head_and_base(tmp_path, api, entry, fault):
    run = api["routes"][
        f"{PREFIX}/actions/workflows/ci.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    if fault == "missing":
        del run["pull_requests"]
    elif fault == "number":
        run["pull_requests"][0]["number"] = 999
    else:
        run["pull_requests"][0][fault]["sha"] = "c" * 40
    process, calls = run_cli(
        tmp_path,
        api,
        mode="merge",
        sweep=entry == "merge",
        approval=entry == "approval",
        readiness=entry == "readiness",
    )
    assert process.returncode == (1 if entry == "readiness" else 0)
    assert "CI did not test the current PR head and base" in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize(
    "fault", [None, "different_pin", "unmapped_only", "commands", "credential"]
)
def test_tested_action_transition_covers_other_occurrences(tmp_path, expanded_update, fault):
    _, api = expanded_update
    pair = api["files"][".github/workflows/ci.yml"]
    files = {".github/workflows/ci.yml": pair, ".github/workflows/release.yml": pair}
    if fault == "different_pin":
        files[".github/workflows/release.yml"] = (pair[0], pair[1].replace("2" * 40, "3" * 40))
    elif fault == "unmapped_only":
        del files[".github/workflows/ci.yml"]
    elif fault == "commands":
        files[".github/workflows/release.yml"] = (
            pair[0],
            pair[1] + "      - run: arbitrary-command\n",
        )
    if fault == "credential":
        files = {
            path: tuple(text.replace("actions/checkout", "docker/login-action") for text in pair)
            for path, pair in files.items()
        }
    set_changed_files(api, files)
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (fault is None), process.stdout
    if fault == "credential":
        assert "docker/login-action requires human review" in process.stdout


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize("action", ["docker/build-push-action", "example/unlisted-action"])
def test_action_evidence_does_not_extend_to_unmapped_publishers(
    evaluate_fixture, policy_module, action
):
    api, evaluate = evaluate_fixture
    files = {}
    for name in ("docker-build.yml", "oss-publish-images.yml"):
        path = f".github/workflows/{name}"
        old = (ROOT / path).read_text()
        pin = "c3c9e263c25d99ce0380d002d59b67737d91b0dc"
        new = old.replace(f"docker/build-push-action@{pin}", f"{action}@{'2' * 40}")
        old = old.replace("docker/build-push-action@", action + "@")
        assert old != new
        files[path] = old, new
    api["files"] = files
    # Avoid making release validation the reason an uncovered occurrence fails.
    policy_module.github_release = lambda _repository, sha, _days: (
        "7.4.1" if sha == "2" * 40 else "7.4.0",
        "",
    )
    reason = (
        "docker/build-push-action requires human review"
        if action == "docker/build-push-action"
        else "has an unmapped occurrence requiring human review"
    )
    with pytest.raises(policy_module.NeedsReview, match=reason):
        evaluate()


@pytest.mark.parametrize("expanded_update", ["npm"], indirect=True)
@pytest.mark.parametrize("consumer", ["none", "workspace", "transitive", "alias", "optional"])
def test_npm_prunes_only_unreachable_entries(tmp_path, expanded_update, consumer):
    _ecosystem, api = expanded_update
    files = api["files"]
    locks = [yaml.safe_load(text) for text in files["pnpm-lock.yaml"]]
    locks[0]["packages"]["marked@18.0.6"] = {"resolution": {"integrity": "sha512-unused"}}
    locks[0]["snapshots"]["marked@18.0.6"] = {}
    if consumer in {"workspace", "alias"}:
        for lock in locks:
            name, pin = (
                ("old-marked", "marked@18.0.6") if consumer == "alias" else ("marked", "18.0.6")
            )
            lock["importers"]["another-workspace"] = {
                "dependencies": {name: {"specifier": "^18.0.6", "version": pin}}
            }
    elif consumer in {"transitive", "optional"}:
        for lock, pin in zip(locks, ["2.1.0", "2.1.1"], strict=True):
            section = "optionalDependencies" if consumer == "optional" else "dependencies"
            lock["snapshots"][f"clsx@{pin}"] = {section: {"marked": "18.0.6"}}
    files["pnpm-lock.yaml"] = tuple(yaml.safe_dump(lock) for lock in locks)
    set_changed_files(api, files)
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (consumer == "none"), (
        process.stdout
    )


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize("fault", [None, "major", "young", "untested", "command", "fork"])
def test_scan_pin_exception_is_separate_from_merge_authorization(tmp_path, expanded_update, fault):
    _ecosystem, api = expanded_update
    files = api["files"]
    old, new = files[".github/workflows/ci.yml"]
    if fault == "major":
        new = new.replace("v4.2.0", "v5.0.0")
        api["routes"]["repos/actions/checkout/tags?per_page=100&page=1"][1]["name"] = "v5.0.0"
        api["routes"]["repos/actions/checkout/releases/tags/v5.0.0"] = api["routes"][
            "repos/actions/checkout/releases/tags/v4.2.0"
        ]
    elif fault == "young":
        api["routes"]["repos/actions/checkout/releases/tags/v4.2.0"]["published_at"] = (
            datetime.now(UTC).isoformat()
        )
    elif fault == "command":
        new += "      - run: echo unexpected\n"
    path = ".github/workflows/release.yml" if fault == "untested" else ".github/workflows/ci.yml"
    set_changed_files(api, {path: (old, new)})
    if fault == "fork":
        api["routes"][f"{PREFIX}/pulls/123"]["head"]["repo"]["full_name"] = "fork/project"
    scan_dir, merge_dir = tmp_path / "scan", tmp_path / "merge"
    scan_dir.mkdir()
    merge_dir.mkdir()
    process, _ = run_cli(scan_dir, api, mode="report", scan=True)
    assert process.returncode == 0, process.stderr
    permitted = fault not in {"command", "fork"}
    assert (scan_dir / "outputs").read_text() == f"workflow_pins={str(permitted).lower()}\n"
    process, calls = run_cli(merge_dir, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (fault is None), process.stdout


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize("action", ["e2e-run", "integration-run", "setup-uv"])
def test_local_action_updates_require_executed_consumer_jobs(tmp_path, expanded_update, action):
    _ecosystem, api = expanded_update
    old, new = api["files"][".github/workflows/ci.yml"]
    set_changed_files(api, {f".github/actions/{action}/action.yml": (old, new)})
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert calls[-1][:2] == ["pr", "merge"], process.stdout
    policy = json.loads((ROOT / ".github/dependabot-automerge.json").read_text())
    workflow = policy["action_test_paths"][f".github/actions/{action}/action.yml"]
    run = api["routes"][
        f"{PREFIX}/actions/workflows/{workflow}/runs?head_sha={HEAD}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    for job in api["routes"][f"{PREFIX}/actions/runs/{run['id']}/jobs?per_page=100&page=1"][
        "jobs"
    ]:
        job["conclusion"] = "skipped"
    blocked = tmp_path / "skipped"
    blocked.mkdir()
    process, calls = run_cli(blocked, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert not any(call[:2] == ["pr", "merge"] for call in calls), process.stdout


@pytest.mark.parametrize("newest", ["label", "failure", "pending", "missing_jobs"])
def test_skipped_label_run_does_not_hide_real_e2e_result(tmp_path, api, newest):
    runs = api["routes"][
        f"{PREFIX}/actions/workflows/e2e.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
    ]["workflow_runs"]
    runs.append(dict(runs[0], id=80))
    jobs = [{"name": "gate / Security Gate", "conclusion": "skipped"}]
    if newest == "failure":
        runs[-1].update(conclusion="failure", run_attempt=1)
        jobs = [{"name": "E2E Tests (shard 1/1)", "conclusion": "failure"}]
    elif newest == "pending":
        runs[-1].update(status="in_progress", conclusion=None)
    elif newest == "missing_jobs":
        jobs = []
    api["routes"][f"{PREFIX}/actions/runs/80/jobs?per_page=100&page=1"] = {"jobs": jobs}
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (newest == "label"), (
        process.stdout
    )


@pytest.mark.parametrize(
    "condition",
    ["eligible", "report", "held", "pending", "limit", "wrong_workflow", "reports_pending"],
)
def test_worker_refreshes_stale_approval_without_merging(tmp_path, api, condition):
    check = api["routes"]["Maintainer Approval"]["check_runs"][0]
    check.update(
        conclusion="failure", details_url=f"https://github.com/{REPO}/actions/runs/90/job/91"
    )
    run = {
        "path": ".github/workflows/maintainer-approval.yml",
        "event": "pull_request_target",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
    }
    api["routes"][f"{PREFIX}/actions/runs/90"] = run
    if condition == "held":
        api["routes"][f"{PREFIX}/pulls/123"]["labels"] = [{"name": "hold-automerge"}]
    elif condition == "pending":
        check.update(status="in_progress", conclusion=None)
    elif condition == "limit":
        run["run_attempt"] = 3
    elif condition == "wrong_workflow":
        run["path"] = ".github/workflows/release.yml"
    elif condition == "reports_pending":
        api["routes"][
            f"{PREFIX}/actions/workflows/ci.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
        ]["workflow_runs"][0]["status"] = "in_progress"
    process, calls = run_cli(
        tmp_path, api, mode="report" if condition == "report" else "merge", sweep=True
    )
    assert process.returncode == 0, process.stderr
    posts = [call for call in calls if call[:3] == ["api", "--method", "POST"]]
    assert posts == (
        [["api", "--method", "POST", f"{PREFIX}/actions/runs/90/rerun-failed-jobs"]]
        if condition == "eligible"
        else []
    ), process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)
    if condition == "eligible":
        check["conclusion"] = "success"
        followup = tmp_path / "next-sweep"
        followup.mkdir()
        process, calls = run_cli(followup, api, mode="merge", sweep=True)
        assert calls[-1][:2] == ["pr", "merge"], process.stdout


@pytest.mark.parametrize("workflow", ["ci.yml", "windows.yml"])
@pytest.mark.parametrize("condition", ["gate", "test", "scan_red", "report", "limit"])
def test_worker_only_recovers_ci_blocked_by_a_now_green_scan(tmp_path, api, condition, workflow):
    run = api["routes"][
        f"{PREFIX}/actions/workflows/{workflow}/runs?head_sha={HEAD}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    run.update(conclusion="failure", run_attempt=3 if condition == "limit" else 1)
    jobs = api["routes"][f"{PREFIX}/actions/runs/{run['id']}/jobs?per_page=100&page=1"]["jobs"]
    jobs[0]["conclusion"] = "failure" if condition == "test" else "skipped"
    jobs.append({"name": "gate / Security Gate", "conclusion": "failure"})
    if condition == "scan_red":
        api["routes"]["Security Scan"]["check_runs"][0]["conclusion"] = "failure"
    process, calls = run_cli(
        tmp_path, api, mode="report" if condition == "report" else "merge", sweep=True
    )
    assert process.returncode == 0, process.stderr
    posts = [call for call in calls if call[:3] == ["api", "--method", "POST"]]
    assert posts == (
        [["api", "--method", "POST", f"{PREFIX}/actions/runs/{run['id']}/rerun"]]
        if condition == "gate"
        else []
    ), process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize("state", ["missing", "pending", "failure"])
@pytest.mark.parametrize("mode", ["merge", "report"])
def test_merge_ready_is_a_commit_status_not_a_job_check(tmp_path, api, state, mode):
    statuses = api["routes"][f"{PREFIX}/commits/{HEAD}/statuses?per_page=100&page=1"]
    if state == "missing":
        statuses.clear()
    else:
        statuses.append({"id": 2, "context": "Merge Ready", "state": state})
    process, calls = run_cli(tmp_path, api, mode=mode, sweep=True)
    assert process.returncode == 0, process.stderr
    assert "Waiting for successful Merge Ready status" in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)

    assert any(call[:2] == ["workflow", "run"] for call in calls) == (mode == "merge")


def test_report_mode_checks_advisories_without_automatic_approval(tmp_path, api):
    del api["routes"]["Maintainer Approval"]
    api["report"]["Results"][0]["Vulnerabilities"].append(
        {
            "PkgName": "ruff",
            "InstalledVersion": "0.15.2",
            "Severity": "HIGH",
            "VulnerabilityID": "CVE-changed-version",
        }
    )
    process, calls = run_cli(tmp_path, api, mode="report", sweep=True)
    assert process.returncode == 0, process.stderr
    assert "CVE-changed-version requires human review" in process.stdout
    assert not any(call[:3] == ["api", "--method", "POST"] for call in calls)
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize(
    "expanded_update,run_id,marker",
    [
        (ecosystem, 12, "CI coverage scope")
        for ecosystem in ("python", "npm", "cargo", "github-actions")
    ]
    + [
        ("npm", 15, "E2E coverage scope"),
        ("npm", 16, "Web coverage scope"),
        ("python", 17, "UI coverage scope"),
    ],
    indirect=["expanded_update"],
)
@pytest.mark.parametrize("result", ["missing", "skipped", "failure"])
def test_full_dependency_coverage_marker_blocks_older_runs(
    tmp_path, expanded_update, result, run_id, marker
):
    _ecosystem, api = expanded_update
    jobs = api["routes"][f"{PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1"]["jobs"]
    for job in list(jobs):
        if job["name"] == marker:
            if result == "missing":
                jobs.remove(job)
            else:
                job["conclusion"] = result
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert not any(call[:2] == ["pr", "merge"] for call in calls), process.stdout
    assert f"waiting for successful {marker}" in process.stdout


@pytest.mark.parametrize("expanded_update", ["python", "github-actions"], indirect=True)
@pytest.mark.parametrize("result", ["success", "skipped", "failure"])
def test_dependency_automerge_requires_windows_hard_checks(tmp_path, expanded_update, result):
    _ecosystem, api = expanded_update
    jobs = api["routes"][f"{PREFIX}/actions/runs/19/jobs?per_page=100&page=1"]["jobs"]
    jobs[0]["conclusion"] = result
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (result == "success"), (
        process.stdout
    )


@pytest.mark.parametrize("entry", ["approval", "readiness"])
@pytest.mark.parametrize("mode", ["report", "merge"])
@pytest.mark.parametrize("review", ["current", "stale", "outsider", "dismissed"])
def test_manual_dependency_review_uses_existing_approval_rules(tmp_path, api, entry, mode, review):
    api["routes"][f"{PREFIX}/pulls/123/reviews?per_page=100&page=1"] = [
        {
            "user": {"login": "outsider" if review == "outsider" else "maintainer"},
            "state": "DISMISSED" if review == "dismissed" else "APPROVED",
            "commit_id": BASE if review == "stale" else HEAD,
        }
    ]
    api["routes"][f"{PREFIX}/pulls/123"]["labels"] = [{"name": "skip-security-scan"}]
    api["pypi_timeout"] = True
    api["ci_eval"] = "failure"
    api["post_red"] = "true"
    process, calls = run_cli(
        tmp_path, api, mode=mode, approval=entry == "approval", readiness=entry == "readiness"
    )
    automatic_evidence_required = entry == "approval" or mode == "merge"
    expected = 1 if automatic_evidence_required and review != "current" else 0
    assert process.returncode == expected, process.stdout
    assert not (tmp_path / "pypi-called").exists()
    if entry == "readiness":
        # A human can waive the automatic policy, but cannot waive required CI.
        assert "state=failure" in calls[-1]
    assert not any("state=pending" in call for call in calls)


def test_dependency_readiness_does_not_wait_on_either_gate(tmp_path, api):
    del api["routes"]["Maintainer Approval"]
    del api["routes"][f"{PREFIX}/commits/{HEAD}/statuses?per_page=100&page=1"]
    process, calls = run_cli(tmp_path, api, mode="merge", readiness=True)
    assert process.returncode == 0, process.stdout + process.stderr
    assert "state=success" in calls[-1]
    assert not any(
        "check_name=Maintainer" in str(call)
        or ("/commits/" in str(call) and "/statuses" in str(call))
        for call in calls
    )


@pytest.mark.parametrize("status_exit", [0, 1])
def test_auto_approval_invalidates_legacy_readiness_before_succeeding(tmp_path, api, status_exit):
    api["status_exit"] = status_exit
    process, calls = run_cli(tmp_path, api, mode="merge", approval=True)
    assert process.returncode == status_exit, process.stdout + process.stderr
    assert calls[-1] == [
        "api",
        f"{PREFIX}/statuses/{HEAD}",
        "-f",
        "state=pending",
        "-f",
        "context=Merge Ready",
        "-f",
        "description=Rechecking dependency merge readiness",
    ]


def test_ordinary_pr_readiness_never_loads_dependency_policy_or_reviews(tmp_path, api):
    pull = api["routes"][f"{PREFIX}/pulls/123"]
    pull["user"] = {"login": "contributor", "type": "User"}
    api["routes"] = {f"{PREFIX}/pulls/123": pull}
    process, calls = run_cli(tmp_path, api, readiness=True)
    assert process.returncode == 0, process.stdout + process.stderr
    assert calls[0] == ["api", f"{PREFIX}/pulls/123"]
    assert len(calls) == 2 and "state=success" in calls[-1]


def test_dependency_readiness_workflow_covers_evidence_and_authorization_events():
    workflow = yaml.safe_load((ROOT / ".github/workflows/merge-ready.yml").read_text())
    events = workflow[True]
    assert events["pull_request_target"]["types"] == ["labeled", "unlabeled"]
    subscribed = set(events["workflow_run"]["workflows"])
    policy = json.loads((ROOT / ".github/dependabot-automerge.json").read_text())
    for name in [*policy["workflow_test_jobs"], "security-scan.yml", "maintainer-approval.yml"]:
        evidence = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        assert evidence["name"] in subscribed
    job = workflow["jobs"]["evaluate"]
    assert "github.event.workflow_run.event == 'pull_request_target'" in job["if"]
    assert "github.event.workflow_run.name == 'Maintainer Approval'" in job["if"]
    for label in ["hold-automerge", "skip-security-scan"]:
        assert label in job["if"]
    steps = job["steps"]
    checkout = next(s for s in steps if "uses" in s)
    assert checkout["with"]["ref"] == "main"
    assert not checkout["with"]["persist-credentials"]
    for source in [
        "maintainer_approval.py",
        "dependabot_automerge.py",
        "dependabot_policy.py",
        "dependabot-automerge.json",
    ]:
        assert source in checkout["with"]["sparse-checkout"]
    gate = next(s for s in steps if s.get("id") == "gate")
    assert gate["env"]["DEPENDENCY_EVAL"] == "${{ steps.dependency.outcome }}"
    post = next(s for s in steps if s["name"] == "Post Merge Ready status on PR head SHA")
    assert "steps.gate.outputs.post_status == 'true'" in post["if"]
    dependency = next(s for s in steps if s.get("id") == "dependency")
    assert "steps.labels.outputs.dependency_required == 'true'" in dependency["if"]
    assert "parser.outcome" not in dependency["if"]
    approval_workflow = yaml.safe_load(
        (ROOT / ".github/workflows/maintainer-approval.yml").read_text()
    )
    assert approval_workflow["jobs"]["approve"]["permissions"]["statuses"] == "write"
    approval_step = next(
        s
        for s in approval_workflow["jobs"]["approve"]["steps"]
        if s.get("name") == "Require maintainer approval"
    )
    for parser_step in (
        next(s for s in steps if s.get("id") == "parser"),
        next(
            s
            for s in approval_workflow["jobs"]["approve"]["steps"]
            if s.get("name") == "Install trusted policy parser"
        ),
    ):
        assert parser_step["continue-on-error"]
    evidence_step = next(s for s in steps if s.get("id") == "dependency")
    for flag in [
        "--trusted-author 'omni-resolve-agent[bot]'",
        "--trusted-successor 'omni-resolve-agent[bot]'",
    ]:
        assert flag in approval_step["run"] and flag in evidence_step["run"]


@pytest.mark.parametrize("entry", ["approval", "readiness"])
@pytest.mark.parametrize("review", [True, False])
def test_missing_parser_preserves_manual_readiness_and_denies_automatic_exemption(
    tmp_path, api, review, entry
):
    api["parser_missing"] = True
    if review:
        api["routes"][f"{PREFIX}/pulls/123/reviews?per_page=100&page=1"] = [
            {"user": {"login": "maintainer"}, "state": "APPROVED", "commit_id": HEAD}
        ]
    process, calls = run_cli(
        tmp_path, api, mode="merge", readiness=entry == "readiness", approval=entry == "approval"
    )
    assert process.returncode == (0 if review else 1), process.stdout + process.stderr
    if entry == "readiness":
        assert ("state=success" if review else "state=failure") in calls[-1]
    else:
        assert not any("state=pending" in call for call in calls)
    assert (
        "Current head approved" if review else "simulated unavailable PyYAML"
    ) in process.stdout
    assert not (tmp_path / "pypi-called").exists()


@pytest.mark.parametrize("author,mode", [("contributor", "merge"), ("dependabot[bot]", "report")])
@pytest.mark.parametrize("fault", ["ci_red", "api_failure"])
def test_ordinary_and_report_mode_readiness_do_not_post_incidental_failures(
    tmp_path, api, author, mode, fault
):
    api["routes"][f"{PREFIX}/pulls/123"]["user"]["login"] = author
    api["ci_eval"] = "failure"
    if fault == "api_failure":
        api["pull_api_exit"] = 1
    process, calls = run_cli(tmp_path, api, mode=mode, readiness=True)
    assert process.returncode == (1 if fault == "api_failure" else 0), process.stderr
    assert calls == [["api", f"{PREFIX}/pulls/123"]]
    assert not (tmp_path / "pypi-called").exists()


@pytest.mark.parametrize("post_red", ["true", "false"])
def test_successful_dependency_evidence_preserves_existing_ci_posting_policy(
    tmp_path, api, post_red
):
    api["ci_eval"] = "failure"
    api["post_red"] = post_red
    process, calls = run_cli(tmp_path, api, mode="merge", readiness=True)
    assert process.returncode == 0, process.stdout + process.stderr
    posts = [c for c in calls if c[:2] == ["api", f"{PREFIX}/statuses/{HEAD}"]]
    assert len(posts) == (1 if post_red == "true" else 0)
    if posts:
        assert "state=failure" in posts[0]


@pytest.mark.parametrize(
    "condition", ["running", "limit", "available", "other_pr", "other_head", "incomplete"]
)
def test_worker_bounds_readiness_dispatch_for_each_head(tmp_path, api, condition):
    api["routes"][f"{PREFIX}/commits/{HEAD}/statuses?per_page=100&page=1"] = []
    title = f"Merge Ready #123 ({HEAD})"
    if condition == "other_pr":
        title = f"Merge Ready #456 ({HEAD})"
    elif condition == "other_head":
        title = f"Merge Ready #123 ({BASE})"
    runs = [{"display_title": title, "status": "completed"} for _ in range(3)]
    if condition == "running":
        runs = [{"display_title": title, "status": "in_progress"}]
    elif condition == "available":
        runs = runs[:2]
    api["readiness_dispatches"] = {
        "total_count": 101 if condition == "incomplete" else len(runs),
        "workflow_runs": runs,
    }
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(c[:2] == ["workflow", "run"] for c in calls) == (
        condition in {"available", "other_pr", "other_head"}
    )
    reason = {
        "running": "refresh is already running",
        "limit": "refresh limit reached",
        "incomplete": "refresh history is incomplete",
    }.get(condition, "Waiting for successful Merge Ready")
    assert reason in process.stdout
    assert not any(c[:2] == ["pr", "merge"] for c in calls)
    workflow = yaml.safe_load((ROOT / ".github/workflows/dependabot-automerge.yml").read_text())
    mint_token = next(
        s for s in workflow["jobs"]["evaluate"]["steps"] if s.get("id") == "merge-token"
    )
    assert mint_token["with"]["permission-actions"] == "write"
    readiness = yaml.safe_load((ROOT / ".github/workflows/merge-ready.yml").read_text())
    assert "format('Merge Ready #{0} ({1})', inputs.pr, inputs.sha)" in readiness["run-name"]


@pytest.mark.parametrize(
    "maximum,merge_count,reason",
    [
        (4, 3, None),
        (4, 4, "Base refresh limit reached (4/4)"),
        (0, 0, "Base refresh limit reached (0/0)"),
        (None, 0, "max_base_refreshes must be a non-negative integer"),
        (-1, 0, "max_base_refreshes must be a non-negative integer"),
        (True, 0, "max_base_refreshes must be a non-negative integer"),
        ("20", 0, "max_base_refreshes must be a non-negative integer"),
        (2.5, 0, "max_base_refreshes must be a non-negative integer"),
    ],
)
def test_base_refresh_budget_uses_trusted_policy(
    tmp_path, monkeypatch, policy_module, maximum, merge_count, reason
):
    worker = importlib.import_module("dependabot_automerge")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"max_base_refreshes": maximum}))
    monkeypatch.setattr(worker, "POLICY", policy_file)
    commits = [{"parents": [{"sha": BASE}]}] + [
        {"parents": [{"sha": BASE}, {"sha": "c" * 40}]} for _ in range(merge_count)
    ]
    if reason is None:
        worker.require_base_refresh_budget(commits)
    else:
        with pytest.raises(ValueError, match=re.escape(reason)):
            worker.require_base_refresh_budget(commits)


@pytest.mark.parametrize(
    "condition",
    [
        "green",
        "report",
        "failed",
        "pending",
        "advisory",
        "limit",
        "incomplete",
        "hold",
        "head_moved",
        "two_merges",
        "nineteen_merges",
        "two_pages",
        "limit_two_pages",
        "incomplete_two_pages",
        "check_failed",
        "check_pending",
        "mixed_bases",
        "wrong_tip",
        "held_at_refresh",
        "objection_at_refresh",
        "update_rejected",
        "update_rejected_without_http",
        "approval_pending",
        "approval_unrecoverable",
        "approval_recoverable",
        "optional_oidc",
        "reusable_oidc",
        "main_oidc",
        "workflow_inventory",
        "missing_branch",
    ],
)
def test_worker_refreshes_only_green_stale_candidates(tmp_path, api, condition):
    tip = "c" * 40
    workflow = {
        "on": {"pull_request": None},
        "permissions": {"contents": "read"},
        "jobs": {"gate": {"uses": "./.github/workflows/security-gate.yml"}},
    }
    gate = (ROOT / ".github/workflows/security-gate.yml").read_text()
    optional = yaml.safe_load((ROOT / ".github/workflows/release-omnigent.yml").read_text())
    optional["on"] = optional.pop(True)
    optional["on"]["pull_request"]["paths"] = (
        ["uv.lock"] if condition == "optional_oidc" else ["release-only.txt"]
    )
    if condition == "reusable_oidc":
        gate = gate.replace("  contents: read", "  contents: read\n  id-token: write", 1)
    inventory = {
        "uv.lock": (LOCK, LOCK.replace("0.15.1", "0.15.2")),
        ".github/workflows/ci.yml": (yaml.safe_dump(workflow), yaml.safe_dump(workflow)),
        ".github/workflows/security-gate.yml": (gate, gate),
        ".github/workflows/release-omnigent.yml": (
            yaml.safe_dump(optional),
            yaml.safe_dump(optional),
        ),
    }
    set_changed_files(api, inventory)
    main_files = {path: pair[0] for path, pair in inventory.items()}
    if condition == "main_oidc":
        workflow["permissions"]["id-token"] = "write"
        main_files[".github/workflows/ci.yml"] = yaml.safe_dump(workflow)
    set_git_files(api, tip, main_files)
    if condition == "workflow_inventory":
        api["routes"][f"{PREFIX}/git/trees/{tip}/.github/workflows"]["truncated"] = True
    api["routes"][f"{PREFIX}/git/ref/heads/main"]["object"]["sha"] = tip
    api["routes"][f"{PREFIX}/compare/{tip}...{HEAD}"] = {"merge_base_commit": {"sha": BASE}}
    pull = api["routes"][f"{PREFIX}/pulls/123"]
    if condition == "missing_branch":
        del pull["head"]["ref"]
    pull["commits"] = 1
    commits = [{"sha": HEAD, "parents": [{"sha": BASE}]}]
    if condition in {"limit", "two_merges", "green", "nineteen_merges"}:
        merge_count = {"limit": 20, "two_merges": 2, "green": 3, "nineteen_merges": 19}[condition]
        commits = [
            {"sha": f"{index:040x}", "parents": [{"sha": BASE}, {"sha": tip}]}
            for index in range(merge_count)
        ] + commits
        pull["commits"] = len(commits)
    elif condition in {"two_pages", "limit_two_pages", "incomplete_two_pages"}:
        commits = [
            {"sha": f"{index:040x}", "parents": [{"sha": BASE}]} for index in range(100)
        ] + commits
        for index in (0, 100) if condition != "limit_two_pages" else (*range(19), 100):
            commits[index]["parents"].append({"sha": tip})
        pull["commits"] = len(commits)
        api["routes"][f"{PREFIX}/pulls/123/commits?per_page=100&page=2"] = (
            [] if condition == "incomplete_two_pages" else commits[100:]
        )
        commits = commits[:100]
    elif condition == "incomplete":
        pull["commits"] = 250
    elif condition == "hold":
        pull["labels"] = [{"name": "hold-automerge"}]
    elif condition in {"head_moved", "held_at_refresh"}:
        api["pull_after"] = copy.deepcopy(pull)
        api["pull_after_reads"] = 2
        if condition == "head_moved":
            api["pull_after"]["head"]["sha"] = "d" * 40
        else:
            api["pull_after"]["labels"] = [{"name": "hold-automerge"}]
    elif condition == "mixed_bases":
        run = api["routes"][
            f"{PREFIX}/actions/workflows/e2e.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
        ]["workflow_runs"][0]
        run["pull_requests"][0]["base"]["sha"] = "d" * 40
    elif condition == "wrong_tip":
        commits[-1]["sha"] = "d" * 40
    elif condition == "objection_at_refresh":
        api["reviews_after"] = [{"user": {"login": "maintainer"}, "state": "CHANGES_REQUESTED"}]
    elif condition in {"update_rejected", "update_rejected_without_http"}:
        api["update_exit"] = 1
        if condition == "update_rejected_without_http":
            api["update_error"] = "opaque failure with private diagnostic text"
    elif condition in {"check_failed", "check_pending"}:
        api["routes"]["Pre-commit checks"]["check_runs"][0].update(
            status="in_progress" if condition == "check_pending" else "completed",
            conclusion="failure" if condition == "check_failed" else None,
        )
    elif condition.startswith("approval_"):
        check = api["routes"]["Maintainer Approval"]["check_runs"][0]
        if condition == "approval_pending":
            check.update(status="in_progress", conclusion=None)
        else:
            check.update(
                conclusion="failure",
                details_url=f"https://github.com/{REPO}/actions/runs/90/job/1",
            )
            api["routes"][f"{PREFIX}/actions/runs/90"] = {
                "path": ".github/workflows/maintainer-approval.yml",
                "event": "pull_request_target",
                "status": "completed",
                "conclusion": "failure",
                "run_attempt": 3 if condition == "approval_unrecoverable" else 1,
            }
    elif condition in {"failed", "pending"}:
        run = api["routes"][
            f"{PREFIX}/actions/workflows/e2e.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
        ]["workflow_runs"][0]
        run.update(
            status="in_progress" if condition == "pending" else "completed",
            conclusion="failure" if condition == "failed" else None,
        )
    elif condition == "advisory":
        api["report"]["Results"][0]["Vulnerabilities"].append(
            {
                "PkgName": "ruff",
                "InstalledVersion": "0.15.2",
                "Severity": "HIGH",
                "VulnerabilityID": "CVE-update",
            }
        )
    api["routes"][f"{PREFIX}/pulls/123/commits?per_page=100&page=1"] = commits
    process, calls = run_cli(
        tmp_path, api, mode="report" if condition == "report" else "merge", sweep=True
    )
    assert process.returncode == 0, process.stderr
    updates = [call for call in calls if call[:3] == ["api", "--method", "PUT"]]
    assert bool(updates) == (
        condition
        in {
            "green",
            "two_merges",
            "nineteen_merges",
            "two_pages",
            "update_rejected",
            "update_rejected_without_http",
            "approval_recoverable",
        }
    ), process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)
    assert not any(call[:3] == ["api", "--method", "POST"] for call in calls)
    reason = {
        "green": "waiting for fresh policy, scans and CI",
        "two_merges": "waiting for fresh policy, scans and CI",
        "nineteen_merges": "waiting for fresh policy, scans and CI",
        "two_pages": "waiting for fresh policy, scans and CI",
        "approval_recoverable": "waiting for fresh policy, scans and CI",
        "mixed_bases": "one identifiable base",
        "wrong_tip": "Incomplete commit inventory",
        "held_at_refresh": "PR was put on hold",
        "objection_at_refresh": "A reviewer has requested changes",
        "update_rejected": "GitHub rejected branch refresh (HTTP 422)",
        "update_rejected_without_http": "GitHub rejected branch refresh; inspect",
        "check_failed": "Waiting for successful Pre-commit checks",
        "check_pending": "Waiting for successful Pre-commit checks",
        "failed": "Latest e2e.yml run did not succeed",
        "pending": "Latest e2e.yml run did not succeed",
        "advisory": "CVE-update requires human review",
        "limit": "Base refresh limit reached (20/20)",
        "limit_two_pages": "Base refresh limit reached (20/20)",
        "incomplete": "Incomplete commit inventory",
        "incomplete_two_pages": "Incomplete commit inventory",
        "hold": "A hold or scan-waiver label is present",
        "head_moved": "PR or main changed during evaluation",
        "report": "CI did not test the current PR head and base",
        "approval_pending": "Waiting for Maintainer Approval",
        "approval_unrecoverable": "Approval refresh retry limit reached",
        "optional_oidc": "release-omnigent.yml: privileged or unknown permissions",
        "reusable_oidc": "security-gate.yml: privileged or unknown permissions",
        "main_oidc": "ci.yml: privileged or unknown permissions",
        "workflow_inventory": "Workflow inventory is incomplete",
        "missing_branch": "Missing PR branch",
    }[condition]
    assert reason in process.stdout
    assert "private diagnostic text" not in process.stdout
    if condition in {"two_pages", "limit_two_pages", "incomplete_two_pages"}:
        assert ["api", f"{PREFIX}/pulls/123/commits?per_page=100&page=2"] in calls


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
def test_shared_checkout_refresh_cannot_enable_release_oidc(tmp_path, expanded_update):
    _, api = expanded_update
    files = {
        path: tuple(
            text.replace("jobs:\n", "permissions: {contents: read}\njobs:\n") for text in pair
        )
        for path, pair in api["files"].items()
    }
    release = (ROOT / ".github/workflows/release-omnigent.yml").read_text()
    files[".github/workflows/release-omnigent.yml"] = tuple(
        re.sub(r"actions/checkout@[a-f0-9]{40}", f"actions/checkout@{pin * 40}", release)
        for pin in ("1", "2")
    )
    set_changed_files(api, files)
    tip = "c" * 40
    set_git_files(api, tip, {path: pair[0] for path, pair in files.items()})
    api["routes"][f"{PREFIX}/git/ref/heads/main"]["object"]["sha"] = tip
    api["routes"][f"{PREFIX}/compare/{tip}...{HEAD}"] = {"merge_base_commit": {"sha": BASE}}
    api["routes"][f"{PREFIX}/pulls/123"]["commits"] = 1
    api["routes"][f"{PREFIX}/pulls/123/commits?per_page=100&page=1"] = [
        {"sha": HEAD, "parents": [{"sha": BASE}]}
    ]
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert "eligible (actions/checkout==4.2.0)" in process.stdout
    assert "release-omnigent.yml: privileged or unknown permissions" in process.stdout
    assert not any(call[:3] == ["api", "--method", "PUT"] for call in calls)
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "read_all",
        "job_write",
        "sarif_oidc",
        "write_all",
        "missing_permissions",
        "environment",
        "secrets",
        "secret_context",
        "secret_case",
        "secret_object",
        "secret_format",
        "secret_quoted_braces",
        "secret_yaml_escape",
        "remote_reusable",
        "missing_reusable",
        "nested_reusable",
        "cycle",
        "invalid_permissions",
    ],
)
def test_refresh_permissions_follow_optional_and_reusable_jobs(policy_module, fault):
    path = (
        ".github/workflows/trivy.yml"
        if fault == "sarif_oidc"
        else ".github/workflows/optional.yml"
    )
    reusable = ".github/workflows/reusable.yml"
    workflow = {
        "on": "pull_request",
        "permissions": {"contents": "read"},
        "jobs": {"scan": {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}},
    }
    job = workflow["jobs"]["scan"]
    definitions = {path: workflow}
    if fault == "read_all":
        workflow["permissions"] = "read-all"
    elif fault == "job_write":
        job["permissions"] = {"id-token": "write"}
    elif fault == "sarif_oidc":
        job["permissions"] = {"security-events": "write", "id-token": "write"}
    elif fault == "write_all":
        workflow["permissions"] = "write-all"
    elif fault == "missing_permissions":
        del workflow["permissions"]
    elif fault == "environment":
        job["environment"] = "test-pypi"
    elif fault == "secrets":
        job["secrets"] = "inherit"
    elif fault == "secret_context":
        workflow["env"] = {"CREDENTIAL": "${{ secrets['CREDENTIAL'] }}"}
    elif fault == "secret_case":
        workflow["env"] = {"CREDENTIAL": "${{ SeCrEtS.CREDENTIAL }}"}
    elif fault == "secret_object":
        workflow["env"] = {"CREDENTIALS": "${{ toJSON(secrets) }}"}
    elif fault == "secret_format":
        workflow["env"] = {"CREDENTIAL": "${{ format('{0}', secrets.CREDENTIAL) }}"}
    elif fault == "secret_quoted_braces":
        workflow["env"] = {"CREDENTIAL": "${{ format('}} {0}', secrets.CREDENTIAL) }}"}
    elif fault == "invalid_permissions":
        job["permissions"] = {"contents": {"unknown": "read"}}
    elif fault in {"remote_reusable", "missing_reusable", "nested_reusable", "cycle"}:
        workflow["jobs"] = {"call": {"uses": "./" + reusable}}
        if fault == "remote_reusable":
            workflow["jobs"]["call"]["uses"] = "example/project/.github/workflows/build.yml@main"
        elif fault != "missing_reusable":
            definitions[reusable] = {
                "on": "workflow_call",
                "permissions": {"contents": "read"},
                "jobs": {"nested": {"uses": "./" + path}}
                if fault == "cycle"
                else {
                    "nested": {"runs-on": "ubuntu-latest", "permissions": {"id-token": "write"}}
                },
            }
    texts = {name: yaml.safe_dump(value) for name, value in definitions.items()}
    if fault == "secret_yaml_escape":
        texts[path] += 'env: {CREDENTIAL: "${{ \\x73ecrets.CREDENTIAL }}"}\n'
    if fault in {None, "read_all"}:
        policy_module.require_refresh_permissions(texts, {"uv.lock"}, "dependabot/uv/ruff")
    else:
        reason = "secret access" if fault.startswith("secret_") else None
        with pytest.raises(policy_module.NeedsReview, match=reason):
            policy_module.require_refresh_permissions(texts, {"uv.lock"}, "dependabot/uv/ruff")


@pytest.mark.parametrize(
    "trigger,paths,expected",
    [
        ({"pull_request": None}, {"uv.lock"}, True),
        ({"pull_request": {"types": ["labeled"]}}, {"uv.lock"}, False),
        ({"pull_request": {"paths": ["release.yml"]}}, {"uv.lock"}, False),
        ({"pull_request": {"paths": ["web/**"]}}, {"web/package.json"}, True),
        ({"pull_request": {"paths": ["web/**"]}}, {"uv.lock"}, False),
        ({"pull_request": {"paths": ["**/*.yml"]}}, {"uv.lock"}, True),
        ({"pull_request": {"paths": ["!uv.lock"]}}, {"uv.lock"}, True),
        ({"pull_request": {"paths-ignore": ["uv.lock"]}}, {"uv.lock"}, True),
        ({"push": None}, {"uv.lock"}, True),
        ({"push": {"branches": ["main"]}}, {"uv.lock"}, False),
        ({"push": {"branches": ["dependabot/**"]}}, {"uv.lock"}, True),
        ({"push": {"tags": ["v*"]}}, {"uv.lock"}, False),
        ({"push": {"paths": ["release.yml"]}}, {"uv.lock"}, True),
        ({"pull_request": {"types": ["labeled"]}, "push": None}, {"uv.lock"}, True),
        ({"workflow_call": None}, {"uv.lock"}, False),
    ],
)
def test_refresh_triggers_exclude_only_proven_inactive_workflows(
    policy_module, trigger, paths, expected
):
    assert (
        policy_module.refresh_workflow_may_run({"on": trigger}, paths, "dependabot/uv/ruff")
        is expected
    )


@pytest.mark.parametrize("expanded_update", ["github-actions"], indirect=True)
@pytest.mark.parametrize("conclusion", ["success", "skipped", "missing", "failure"])
def test_android_action_update_requires_unsigned_build(tmp_path, expanded_update, conclusion):
    _, api = expanded_update
    pair = api["files"][".github/workflows/ci.yml"]
    set_changed_files(api, {".github/workflows/android-bundle.yml": pair})
    api["routes"][
        f"{PREFIX}/actions/workflows/android-bundle.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"
    ] = {
        "workflow_runs": [
            {
                "id": 21,
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "pull_requests": [{"number": 123, "head": {"sha": HEAD}, "base": {"sha": BASE}}],
            }
        ]
    }
    api["routes"][f"{PREFIX}/actions/runs/21/jobs?per_page=100&page=1"] = {
        "jobs": []
        if conclusion == "missing"
        else [{"name": "Build unsigned AAB", "conclusion": conclusion}]
    }
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (conclusion == "success"), (
        process.stdout
    )


def test_consumer_coverage_outside_required_workflows_fails_closed(policy_module):
    worker = importlib.import_module("dependabot_automerge")
    candidate = {
        "head": HEAD,
        "ecosystem": "npm",
        "workflows": ["web-tests.yml"],
        "consumer_steps": [{"workflow": "unrequired.yml"}],
    }
    with pytest.raises(ValueError, match="Consumer coverage references a workflow outside"):
        worker.require_reports(REPO, candidate)
