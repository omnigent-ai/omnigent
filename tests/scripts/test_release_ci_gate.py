"""Exercise the release workflow's CI gate with recorded GitHub API shapes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"
_BASE_SHA = "a" * 40
_REPO = "example/project"


def _check(name: str, state: str, conclusion: str, run_id: int) -> str:
    return f"{name}\t{state}\t{conclusion}\thttps://github.com/{_REPO}/actions/runs/{run_id}/job/1"


def _run_gate(
    tmp_path: Path,
    checks: list[str],
    *,
    fail_api: str = "",
) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["plan"]["steps"]
        if step["name"] == "Assert green CI on the base commit"
    )
    fixtures = {
        "checks": checks,
        "runs": {
            "release.yml": [101, 102],
            "merge-ready.yml": [201],
            "polly-review.yml": [301],
        },
        "fail_api": fail_api,
    }
    fixture_path = tmp_path / "api.json"
    fixture_path.write_text(json.dumps(fixtures))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        """
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

fixtures = json.loads(Path(os.environ["API_FIXTURES"]).read_text())
endpoint = sys.argv[2]
if fixtures["fail_api"] and fixtures["fail_api"] in endpoint:
    print("GitHub API unavailable", file=sys.stderr)
    sys.exit(1)
if "/actions/workflows/" in endpoint:
    workflow = endpoint.split("/actions/workflows/")[1].split("/")[0]
    query = parse_qs(urlsplit(endpoint).query)
    assert query["head_sha"] == [os.environ["BASE_SHA"]]
    ids = fixtures["runs"][workflow]
    if "--paginate" not in sys.argv:
        ids = ids[:1]
    if query.get("status") == ["completed"]:
        ids = [i for i in ids if i != 102]
    print("\\n".join(str(i) for i in ids))
elif "/check-runs?" in endpoint:
    assert os.environ["BASE_SHA"] in endpoint
    assert "--paginate" in sys.argv
    print("\\n".join(fixtures["checks"]))
else:
    raise AssertionError(f"Unexpected GitHub API call: {endpoint}")
"""
    )
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env['PATH']}",
        API_FIXTURES=str(fixture_path),
        GITHUB_REPOSITORY=_REPO,
        GITHUB_RUN_ID="103",
        BASE_SHA=_BASE_SHA,
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"),
    )
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10
    )


@pytest.mark.parametrize(
    "ignored",
    [
        _check("plan", "completed", "failure", 101),
        _check("plan", "in_progress", "-", 102),
        _check("plan", "in_progress", "-", 103),
        _check("evaluate", "completed", "failure", 201),
        _check("Polly AI Review", "in_progress", "-", 301),
    ],
    ids=[
        "previous-release",
        "paginated-running-release",
        "current-release",
        "pr-gate",
        "pr-review",
    ],
)
def test_release_ignores_its_own_and_pr_automation_checks(tmp_path: Path, ignored: str) -> None:
    result = _run_gate(tmp_path, [_check("Pytest", "completed", "success", 401), ignored])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI green" in result.stdout


@pytest.mark.parametrize("name", ["Pytest", "Security Scan", "benchmark", "evaluate", "plan"])
@pytest.mark.parametrize(
    "conclusion", ["failure", "timed_out", "action_required", "startup_failure"]
)
def test_release_still_blocks_real_failed_checks(
    tmp_path: Path, name: str, conclusion: str
) -> None:
    result = _run_gate(tmp_path, [_check(name, "completed", conclusion, 401)])
    assert result.returncode != 0
    assert "Failing check runs" in result.stdout


def test_release_still_waits_for_real_pending_checks(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, [_check("Pytest", "in_progress", "-", 401)])
    assert result.returncode != 0
    assert "Check runs still running" in result.stdout


def test_release_requires_ci_after_filtering(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, [_check("plan", "completed", "success", 101)])
    assert result.returncode != 0
    assert "No check runs found" in result.stdout


def test_release_preserves_accepted_conclusions(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        [
            _check("Pytest", "completed", "success", 401),
            _check("Optional", "completed", "skipped", 402),
            _check("Superseded", "completed", "cancelled", 403),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cancelled (superseded)" in result.stdout


@pytest.mark.parametrize(
    "api", ["release.yml", "merge-ready.yml", "polly-review.yml", "check-runs"]
)
def test_release_fails_closed_when_api_lookup_fails(tmp_path: Path, api: str) -> None:
    result = _run_gate(tmp_path, [_check("Pytest", "completed", "success", 401)], fail_api=api)
    assert result.returncode != 0
    assert "CI green" not in result.stdout
