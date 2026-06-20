from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/merge-ready/compute-gate.sh"

# A representative FAILED bullet list, the shape evaluate-checks.sh emits.
FAILED = "- `E2E Tests (shard 0/4)` (still pending or cancelled)\n"


def _run(
    tmp_path: Path,
    *,
    eval_outcome: str = "failure",
    failed: str = FAILED,
    fork_needs_e2e_approval: str | None = None,
) -> dict[str, str]:
    """Run compute-gate.sh with the given env and parse its GITHUB_OUTPUT.

    The script makes no ``gh`` calls -- it is a pure function of its env -- so we
    just set the inputs and read back ``state`` / ``short_desc`` / ``long_desc``.

    :param fork_needs_e2e_approval: when ``None`` the var is left unset entirely,
        to exercise the ``${FORK_NEEDS_E2E_APPROVAL:-false}`` default (back-compat).
    """
    out_file = tmp_path / "gh_output"
    out_file.touch()

    env = os.environ.copy()
    env.update(
        {
            "EVAL": eval_outcome,
            "FAILED": failed,
            "GITHUB_OUTPUT": str(out_file),
        }
    )
    if fork_needs_e2e_approval is not None:
        env["FORK_NEEDS_E2E_APPROVAL"] = fork_needs_e2e_approval
    else:
        # Drop any ambient value so the None case deterministically exercises
        # the script's `:-false` default (os.environ.copy() could inherit it).
        env.pop("FORK_NEEDS_E2E_APPROVAL", None)

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"script failed: {proc.stderr}"
    return _parse_github_output(out_file.read_text())


def _parse_github_output(text: str) -> dict[str, str]:
    """Parse GITHUB_OUTPUT, honoring both ``k=v`` and ``k<<DELIM ... DELIM``."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            key, _, delim = line.partition("<<")
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[key] = "\n".join(body)
        elif "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
        i += 1
    return out


def test_green_gate_has_no_blocker_for_same_repo(tmp_path: Path) -> None:
    """A green same-repo gate is unchanged: success, no fork-approval blocker."""
    out = _run(tmp_path, eval_outcome="success", fork_needs_e2e_approval="false")
    assert out["state"] == "success"
    assert "merging now" in out["long_desc"]
    assert "maintainer must approve" not in out["long_desc"]


def test_red_gate_has_no_blocker_for_same_repo(tmp_path: Path) -> None:
    """A red same-repo gate lists failures but adds no fork-approval blocker."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_approval="false")
    assert out["state"] == "failure"
    assert "gate not green yet" in out["long_desc"]
    assert "maintainer must approve" not in out["long_desc"]


def test_fork_without_approval_is_blocking(tmp_path: Path) -> None:
    """A fork PR without maintainer approval must be blocked (state=failure).

    Even if all other checks are green (eval=success), the gate stays red
    until a maintainer approves the PR to trigger e2e.
    """
    out = _run(tmp_path, eval_outcome="success", fork_needs_e2e_approval="true")
    assert out["state"] == "failure"
    assert "Awaiting maintainer approval" in out["short_desc"]
    assert "maintainer must approve" in out["long_desc"]


def test_red_gate_on_fork_without_approval_is_still_blocking(tmp_path: Path) -> None:
    """A fork PR with red checks AND no approval is doubly blocked."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_approval="true")
    assert out["state"] == "failure"
    assert "maintainer must approve" in out["long_desc"]
    assert "gate not green yet" in out["long_desc"]


def test_fork_with_approval_uses_normal_gate(tmp_path: Path) -> None:
    """A fork PR that HAS maintainer approval uses the normal CI gate."""
    out = _run(tmp_path, eval_outcome="success", fork_needs_e2e_approval="false")
    assert out["state"] == "success"
    assert "merging now" in out["long_desc"]


def test_short_desc_never_exceeds_140_chars(tmp_path: Path) -> None:
    """The 140-char commit status limit is respected."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_approval="true")
    assert len(out["short_desc"]) <= 140


def test_fork_approval_var_unset_defaults_to_no_blocker(tmp_path: Path) -> None:
    """With FORK_NEEDS_E2E_APPROVAL unset, the script defaults to no blocker
    (the ``:-false`` fallback keeps it safe under ``set -u``)."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_approval=None)
    assert out["state"] == "failure"
    assert "maintainer must approve" not in out["long_desc"]
