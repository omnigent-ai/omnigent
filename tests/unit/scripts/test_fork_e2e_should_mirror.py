from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github/scripts/fork-e2e/should-mirror.sh"


def _run(
    tmp_path: Path,
    *,
    approvers: str = "",
    labels: str = "",
    labeler: str = "",
    maintainers: str = "alice bob",
) -> dict[str, str]:
    """
    Run should-mirror.sh against a mocked ``gh`` and return its outputs.

    The gate checks two paths (approval OR label), making up to three
    ``gh`` calls which the mock answers:

    - ``api repos/{repo}/pulls/{pr}/reviews ...`` -> *approvers*
    - ``pr view {pr} ... --json labels --jq '.labels[].name'`` -> *labels*
    - ``api repos/{repo}/issues/{pr}/events ...`` -> *labeler*

    :param approvers: Space-separated logins the reviews mock returns as
        approvers; empty means no approving reviews.
    :param labels: Space-separated labels currently on the PR; empty means none.
    :param labeler: Login the issue-events mock attributes the gate label to.
    :param maintainers: Space-separated maintainer logins.
    :returns: Parsed ``key=value`` GITHUB_OUTPUT lines.
    """
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        # gh pr view <pr> --repo <repo> --json labels --jq '.labels[].name'
        'if [[ "$1" == "pr" ]]; then [[ -n "$MOCK_LABELS" ]]'
        ' && printf "%s\\n" $MOCK_LABELS; exit 0; fi\n'
        'if [[ "$1" == "api" ]]; then\n'
        '  case "$2" in\n'
        '    *pulls/*reviews*) [[ -n "$MOCK_APPROVERS" ]]'
        ' && printf "%s\\n" $MOCK_APPROVERS; exit 0 ;;\n'
        '    *issues/*events*) [[ -n "$MOCK_LABELER" ]]'
        ' && printf "%s\\n" "$MOCK_LABELER"; exit 0 ;;\n'
        "  esac\n"
        "fi\n"
        'echo "unexpected gh invocation: $*" >&2\n'
        "exit 1\n"
    )
    gh.chmod(0o755)

    out_file = tmp_path / "gh_output"
    out_file.touch()

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "GH_TOKEN": "unused",
            "REPO": "test/repo",
            "PR": "7",
            "MAINTAINERS": maintainers,
            "GITHUB_OUTPUT": str(out_file),
            "MOCK_APPROVERS": approvers,
            "MOCK_LABELS": labels,
            "MOCK_LABELER": labeler,
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
    outputs: dict[str, str] = {}
    for line in out_file.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs


# --- Path 1: maintainer approval ---


def test_approved_by_maintainer_mirrors(tmp_path: Path) -> None:
    """A maintainer's approving review opens the gate."""
    out = _run(tmp_path, approvers="bob")
    assert out["mirror"] == "true"
    assert "approved by maintainer" in out["reason"]


def test_maintainer_match_is_case_insensitive(tmp_path: Path) -> None:
    """Approver vs MAINTAINER comparison is case-insensitive."""
    out = _run(tmp_path, approvers="Bob", maintainers="alice bob")
    assert out["mirror"] == "true"


def test_approved_by_non_maintainer_no_label_does_not_mirror(tmp_path: Path) -> None:
    """An approval from a non-maintainer (and no label) doesn't open the gate."""
    out = _run(tmp_path, approvers="eve", maintainers="alice bob")
    assert out["mirror"] == "false"


def test_multiple_approvers_first_maintainer_wins(tmp_path: Path) -> None:
    """When multiple users approve, the first matching maintainer opens the gate."""
    out = _run(tmp_path, approvers="eve alice", maintainers="alice bob")
    assert out["mirror"] == "true"
    assert "@alice" in out["reason"]


# --- Path 2: e2e-approved label ---


def test_label_applied_by_maintainer_mirrors(tmp_path: Path) -> None:
    """The e2e-approved label applied by a maintainer opens the gate."""
    out = _run(tmp_path, labels="e2e-approved", labeler="bob")
    assert out["mirror"] == "true"
    assert "e2e-approved" in out["reason"]
    assert "maintainer" in out["reason"]


def test_label_applied_by_non_maintainer_does_not_mirror(tmp_path: Path) -> None:
    """The e2e-approved label applied by a non-maintainer doesn't open the gate."""
    out = _run(tmp_path, labels="e2e-approved", labeler="eve", maintainers="alice bob")
    assert out["mirror"] == "false"


def test_label_without_labeler_does_not_mirror(tmp_path: Path) -> None:
    """A label with no attributable labeler doesn't open the gate."""
    out = _run(tmp_path, labels="e2e-approved", labeler="")
    assert out["mirror"] == "false"


# --- Either path ---


def test_approval_takes_precedence_over_label(tmp_path: Path) -> None:
    """When both approval and label are present, approval wins (checked first)."""
    out = _run(tmp_path, approvers="alice", labels="e2e-approved", labeler="bob")
    assert out["mirror"] == "true"
    assert "approved by maintainer" in out["reason"]


# --- Neither path ---


def test_no_approval_no_label_does_not_mirror(tmp_path: Path) -> None:
    """Without approval or label, the gate stays shut."""
    out = _run(tmp_path, approvers="", labels="")
    assert out["mirror"] == "false"
    assert "awaiting" in out["reason"]


def test_no_maintainers_loaded_does_not_mirror(tmp_path: Path) -> None:
    """An empty MAINTAINER list fails closed."""
    out = _run(tmp_path, approvers="bob", maintainers="")
    assert out["mirror"] == "false"
    assert "no maintainers" in out["reason"]
