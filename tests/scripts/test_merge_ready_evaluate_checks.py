"""``evaluate-checks.sh`` re-reads checks still reported running after their workflow ended."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / ".github/scripts/merge-ready"
LAGGING = "E2E Tests (shard 0/4)"

_FAKE_GH = """#!{python}
import json, os, subprocess, sys
from pathlib import Path

state = Path(os.environ["TEST_GATE_DIR"])
if "/check-runs" in sys.argv[2]:
    counter = state / "check_reads"
    read = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(read + 1))
    reads = sorted(state.glob("checks_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    response = json.loads(reads[min(read, len(reads) - 1)].read_text())
else:
    response = json.loads((state / "runs.json").read_text())
jq_filter = sys.argv[sys.argv.index("--jq") + 1]
subprocess.run(["jq", "-r", jq_filter], input=json.dumps(response), text=True, check=True)
"""


def _required() -> list[str]:
    script = f'source "{SCRIPTS}/required.sh"; printf "%s\\n" "${{REQUIRED[@]}}"'
    names = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, timeout=30
    )
    return names.stdout.splitlines()


def _checks(overrides: dict[str, tuple[str, str | None]]) -> dict[str, object]:
    """Every required check green, except the overridden ``name -> (status, conclusion)``."""
    rows = []
    for check_id, name in enumerate(_required(), start=1):
        status, conclusion = overrides.get(name, ("completed", "success"))
        rows.append({"id": check_id, "name": name, "status": status, "conclusion": conclusion})
    return {"check_runs": rows}


def _gate(
    tmp_path: Path,
    reads: list[dict[str, object]],
    *,
    e2e_workflow_done: bool,
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run the gate over successive check-run reads; returns the run and the reads it made."""
    for index, payload in enumerate(reads):
        (tmp_path / f"checks_{index}.json").write_text(json.dumps(payload))
    runs = [
        {
            "name": name,
            "status": "completed" if e2e_workflow_done or name != "E2E Tests" else "in_progress",
            "conclusion": "success" if e2e_workflow_done or name != "E2E Tests" else None,
            "created_at": "2026-10-19T15:00:00Z",
        }
        for name in ("CI", "E2E Tests", "E2E UI Tests", "Integration Tests")
    ]
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
    gh = tmp_path / "gh"
    gh.write_text(_FAKE_GH.format(python=sys.executable))
    gh.chmod(0o755)
    process = subprocess.run(
        ["bash", str(SCRIPTS / "evaluate-checks.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TEST_GATE_DIR": str(tmp_path),
            "REPO": "example/project",
            "SHA": "a" * 40,
            "GITHUB_OUTPUT": str(tmp_path / "gate-output"),
            "MERGE_READY_LAG_DELAY_S": "0",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    return process, int((tmp_path / "check_reads").read_text())


def test_a_check_still_running_after_its_workflow_finished_is_read_again(
    tmp_path: Path,
) -> None:
    stale = _checks({LAGGING: ("in_progress", None)})
    process, reads = _gate(tmp_path, [stale, _checks({})], e2e_workflow_done=True)
    assert process.returncode == 0, process.stdout + process.stderr
    assert reads == 2
    assert "Re-reading in 0s (read 1 of 6)" in process.stdout
    assert "All required checks green" in process.stdout
    # One report for the final read, not one per read.
    assert process.stdout.count("OK      : DCO") == 1


def test_a_check_whose_workflow_is_still_running_is_not_read_again(tmp_path: Path) -> None:
    pending = _checks({LAGGING: ("in_progress", None)})
    process, reads = _gate(tmp_path, [pending], e2e_workflow_done=False)
    assert process.returncode == 1
    assert reads == 1
    assert f"NOT GREEN: {LAGGING}" in process.stdout


def test_reads_stop_at_the_attempt_budget(tmp_path: Path) -> None:
    stale = _checks({LAGGING: ("in_progress", None)})
    process, reads = _gate(tmp_path, [stale], e2e_workflow_done=True, MERGE_READY_LAG_ATTEMPTS="3")
    assert process.returncode == 1
    assert reads == 3
    assert f"NOT GREEN: {LAGGING}" in process.stdout


def test_a_real_failure_is_reported_without_waiting(tmp_path: Path) -> None:
    first = _checks({LAGGING: ("in_progress", None), "Pytest (misc)": ("completed", "failure")})
    process, reads = _gate(tmp_path, [first, _checks({})], e2e_workflow_done=True)
    assert process.returncode == 1
    assert reads == 1
    assert "NOT GREEN: Pytest (misc)  (status=completed, conclusion=failure)" in process.stdout
