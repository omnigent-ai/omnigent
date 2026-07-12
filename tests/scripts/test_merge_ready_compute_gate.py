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
) -> dict[str, str]:
    """Run compute-gate.sh with the given env and parse its GITHUB_OUTPUT.

    The script makes no ``gh`` calls -- it is a pure function of its env -- so we
    just set the inputs and read back ``state`` / ``short_desc`` / ``long_desc``.
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


def test_green_gate_is_success(tmp_path: Path) -> None:
    """A green CI eval yields state=success and the merging-now message."""
    out = _run(tmp_path, eval_outcome="success")
    assert out["state"] == "success"
    assert "merging now" in out["long_desc"]


def test_red_gate_is_failure(tmp_path: Path) -> None:
    """A red CI eval lists the failing checks and yields state=failure."""
    out = _run(tmp_path, eval_outcome="failure")
    assert out["state"] == "failure"
    assert "gate not green yet" in out["long_desc"]


def test_short_desc_never_exceeds_140_chars(tmp_path: Path) -> None:
    """The 140-char commit status limit is respected."""
    out = _run(tmp_path, eval_outcome="failure")
    assert len(out["short_desc"]) <= 140
