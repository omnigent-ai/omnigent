"""Execute the security gate against GitHub's paginated check-run response shape."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
