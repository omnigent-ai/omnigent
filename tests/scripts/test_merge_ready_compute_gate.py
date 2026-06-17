from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/merge-ready/compute-gate.sh"

# A representative FAILED bullet list, the shape evaluate-checks.sh emits.
FAILED = "- `E2E Tests (shard 0/4)` (still pending or cancelled)\n"

# The hint sentinel -- the maintainer-only label name appears only in the nudge.
HINT_MARKER = "e2e-approved"


def _run(
    tmp_path: Path,
    *,
    eval_outcome: str = "failure",
    failed: str = FAILED,
    fork_needs_e2e_label: str | None = None,
) -> dict[str, str]:
    """Run compute-gate.sh with the given env and parse its GITHUB_OUTPUT.

    The script makes no ``gh`` calls -- it is a pure function of its env -- so we
    just set the inputs and read back ``state`` / ``short_desc`` / ``long_desc``.

    :param fork_needs_e2e_label: when ``None`` the var is left unset entirely, to
        exercise the ``${FORK_NEEDS_E2E_LABEL:-false}`` default (back-compat).
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
    if fork_needs_e2e_label is not None:
        env["FORK_NEEDS_E2E_LABEL"] = fork_needs_e2e_label
    else:
        # Drop any ambient value so the None case deterministically exercises
        # the script's `:-false` default (os.environ.copy() could inherit it).
        env.pop("FORK_NEEDS_E2E_LABEL", None)

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


def test_green_gate_has_no_hint_for_same_repo(tmp_path: Path) -> None:
    """A green same-repo gate is unchanged: success, no e2e-label nudge."""
    out = _run(tmp_path, eval_outcome="success", fork_needs_e2e_label="false")
    assert out["state"] == "success"
    assert "merging now" in out["long_desc"]
    assert HINT_MARKER not in out["long_desc"]


def test_red_gate_has_no_hint_for_same_repo(tmp_path: Path) -> None:
    """A red same-repo gate lists failures but adds no e2e-label nudge."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_label="false")
    assert out["state"] == "failure"
    assert "gate not green yet" in out["long_desc"]
    assert HINT_MARKER not in out["long_desc"]


def test_red_gate_on_fork_without_label_adds_hint(tmp_path: Path) -> None:
    """A fork PR missing the label gets the apply-`e2e-approved` nudge appended.

    The failure prose still comes first; the hint is an extra paragraph so the
    contributor/maintainer sees both what's blocking and how to run e2e.
    """
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_label="true")
    assert out["state"] == "failure"
    assert "gate not green yet" in out["long_desc"]
    assert HINT_MARKER in out["long_desc"]
    assert "do not run automatically on fork PRs" in out["long_desc"]


def test_green_gate_on_fork_without_label_adds_hint(tmp_path: Path) -> None:
    """Even a green fork gate carries the hint: green means e2e was skipped,
    not that it ran, so the caveat still matters before merge."""
    out = _run(tmp_path, eval_outcome="success", fork_needs_e2e_label="true")
    assert out["state"] == "success"
    assert "merging now" in out["long_desc"]
    assert HINT_MARKER in out["long_desc"]


def test_short_desc_never_carries_hint(tmp_path: Path) -> None:
    """The hint is comment-only; the 140-char commit status stays clean."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_label="true")
    assert HINT_MARKER not in out["short_desc"]
    assert len(out["short_desc"]) <= 140


def test_fork_label_var_unset_defaults_to_no_hint(tmp_path: Path) -> None:
    """With FORK_NEEDS_E2E_LABEL unset, the script defaults to no hint (the
    ``:-false`` fallback keeps it safe under ``set -u``)."""
    out = _run(tmp_path, eval_outcome="failure", fork_needs_e2e_label=None)
    assert out["state"] == "failure"
    assert HINT_MARKER not in out["long_desc"]
