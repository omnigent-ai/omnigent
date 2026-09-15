from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = REPO_ROOT / ".github/scripts/security-scan/audit-uv-lock.sh"
PR_WORKFLOW = REPO_ROOT / ".github/workflows/security-scan.yml"
SCHEDULED_WORKFLOW = REPO_ROOT / ".github/workflows/dependency-audit.yml"


def _step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - name: ", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def test_audit_exports_both_resolutions_and_filters_editables(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        'printf "uv %s\\n" "$*" >> "$AUDIT_CALLS"\n'
        "printf '%s\\n' '-e .' 'safe-package==1.0'\n",
    )
    _write_executable(
        bin_dir / "uvx",
        "#!/bin/sh\n"
        'printf "uvx %s\\n" "$*" >> "$AUDIT_CALLS"\n'
        'requirements=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--requirement" ]; then requirements="$2"; break; fi\n'
        "  shift\n"
        "done\n"
        'grep -q "^-e " "$requirements" && exit 91\n'
        'grep -q "^safe-package==1.0$" "$requirements"\n',
    )
    env = os.environ.copy()
    env["AUDIT_CALLS"] = str(calls)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    subprocess.run(["bash", str(AUDIT_SCRIPT)], cwd=tmp_path, env=env, check=True)

    recorded = calls.read_text().splitlines()
    assert len([line for line in recorded if line.startswith("uv export ")]) == 2
    assert len([line for line in recorded if line.startswith("uvx pip-audit ")]) == 2
    assert all(
        "--no-deps --disable-pip --vulnerability-service osv" in line
        for line in recorded
        if line.startswith("uvx pip-audit ")
    )
    assert any("--no-extra antigravity --all-groups" in line for line in recorded)
    assert any("--extra antigravity --extra all --no-default-groups" in line for line in recorded)


def test_audit_propagates_pip_audit_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "uv",
        "#!/bin/sh\nprintf '%s\\n' 'vulnerable-package==1.0'\n",
    )
    _write_executable(bin_dir / "uvx", "#!/bin/sh\nexit 1\n")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(["bash", str(AUDIT_SCRIPT)], cwd=tmp_path, env=env)

    assert result.returncode == 1


def test_pr_audit_is_independent_of_author_trust() -> None:
    workflow = PR_WORKFLOW.read_text()

    assert "if:" not in _step(workflow, "Fetch changed files")
    assert "steps.changes.outputs.uv_lock == 'true'" in _step(workflow, "Check out PR head")
    assert "steps.changes.outputs.uv_lock == 'true'" in _step(workflow, "Install uv")
    advisory = _step(workflow, "OSV advisory scan (uv.lock)")
    assert "if: ${{ steps.changes.outputs.uv_lock == 'true' }}" in advisory
    assert "steps.gate.outputs.scan" not in advisory
    assert "steps.advisory.outcome != 'failure'" in _step(
        workflow, "Explain the maintainer waiver (on failure)"
    )


def test_main_has_a_scheduled_dependency_audit() -> None:
    workflow = SCHEDULED_WORKFLOW.read_text()

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "ref: main" in _step(workflow, "Check out main")
    assert "bash .github/scripts/security-scan/audit-uv-lock.sh" in workflow
