from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/fork-e2e/should-mirror.sh"


def _run(
    tmp_path: Path,
    *,
    author_association: str,
    maintainers: str = "",
    branch_exists: bool = False,
    author: str = "octocat",
    approvers: str = "",
) -> dict[str, str]:
    """
    Run should-mirror.sh against a mocked ``gh`` and return its outputs.

    The mock answers the three calls the script can make:

    - ``api repos/{repo}/branches/{mirror_branch}`` -> exit 0 if
      *branch_exists* else exit 1 (the existence probe).
    - ``pr view {pr} ...`` -> *author* (the PR author login).
    - ``api repos/{repo}/pulls/{pr}/reviews ...`` -> *approvers*, a
      space-separated login list printed one per line (post-``--jq``
      shape the script iterates).

    :param tmp_path: Pytest tmp dir for the mock + output file.
    :param author_association: GitHub ``author_association`` value,
        e.g. ``"FIRST_TIME_CONTRIBUTOR"`` or ``"CONTRIBUTOR"``.
    :param maintainers: Space-separated maintainer logins (as
        load-maintainers.sh would emit); empty means none.
    :param branch_exists: Whether fork-e2e/pr-N already exists.
    :param author: PR author login the ``gh pr view`` mock returns.
    :param approvers: Space-separated logins whose latest review is
        APPROVED, as the reviews ``--jq`` would yield.
    :returns: Parsed ``key=value`` GITHUB_OUTPUT lines, e.g.
        ``{"mirror": "true", "reason": "..."}``.
    """
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        'if [[ "$1" == "pr" ]]; then echo "$MOCK_AUTHOR"; exit 0; fi\n'
        'if [[ "$1" == "api" ]]; then\n'
        '  case "$2" in\n'
        '    *branches/*) [[ "$BRANCH_EXISTS" == "1" ]] && exit 0 || exit 1 ;;\n'
        # shellcheck-style: unquoted expansion intentionally splits logins.
        '    *reviews*) [[ -n "$MOCK_APPROVERS" ]] && printf "%s\\n" $MOCK_APPROVERS; exit 0 ;;\n'
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
            "MIRROR_BRANCH": "fork-e2e/pr-7",
            "AUTHOR_ASSOCIATION": author_association,
            "MAINTAINERS": maintainers,
            "GITHUB_OUTPUT": str(out_file),
            "BRANCH_EXISTS": "1" if branch_exists else "0",
            "MOCK_AUTHOR": author,
            "MOCK_APPROVERS": approvers,
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


def test_first_time_contributor_without_approval_does_not_mirror(tmp_path: Path) -> None:
    """A first-timer with no maintainer approval must NOT open the gate.

    This is the whole point of the gate: brand-new contributors don't get a
    secret-bearing e2e run on first push. Asserts ``mirror=false`` (the e2e
    suite stays unmirrored until a maintainer reviews).
    """
    out = _run(tmp_path, author_association="FIRST_TIME_CONTRIBUTOR")
    assert out["mirror"] == "false"


def test_returning_contributor_mirrors(tmp_path: Path) -> None:
    """A returning contributor (author_association=CONTRIBUTOR) auto-mirrors.

    Matches GitHub's own "has contributed before" relaxation. Asserts
    ``mirror=true`` with no maintainer list needed.
    """
    out = _run(tmp_path, author_association="CONTRIBUTOR")
    assert out["mirror"] == "true"
    assert "returning contributor" in out["reason"]


def test_member_association_mirrors(tmp_path: Path) -> None:
    """An org MEMBER's fork PR auto-mirrors (also a returning-type association)."""
    out = _run(tmp_path, author_association="MEMBER")
    assert out["mirror"] == "true"


def test_maintainer_author_mirrors(tmp_path: Path) -> None:
    """A first-timer whose login is in MAINTAINER opens the gate.

    Covers the author-is-maintainer branch even when author_association is not
    a returning value. Asserts ``mirror=true`` and that the reason names the
    author.
    """
    out = _run(
        tmp_path,
        author_association="NONE",
        maintainers="alice bob",
        author="bob",
    )
    assert out["mirror"] == "true"
    assert "maintainer" in out["reason"]


def test_maintainer_approved_review_mirrors(tmp_path: Path) -> None:
    """A first-timer whose PR a maintainer APPROVED opens the gate.

    Covers the review-approval branch: the author isn't a maintainer, but a
    maintainer (``bob``) approved. Asserts ``mirror=true``.
    """
    out = _run(
        tmp_path,
        author_association="FIRST_TIME_CONTRIBUTOR",
        maintainers="alice bob",
        author="newcomer",
        approvers="bob",
    )
    assert out["mirror"] == "true"
    assert "approved by maintainer" in out["reason"]


def test_non_maintainer_approval_does_not_mirror(tmp_path: Path) -> None:
    """An APPROVED review from a NON-maintainer must not open the gate.

    Guards against treating any approval as sufficient: only a maintainer's
    approval counts. ``eve`` approves but isn't in MAINTAINER, so
    ``mirror=false``.
    """
    out = _run(
        tmp_path,
        author_association="FIRST_TIME_CONTRIBUTOR",
        maintainers="alice bob",
        author="newcomer",
        approvers="eve",
    )
    assert out["mirror"] == "false"


def test_existing_branch_always_remirrors(tmp_path: Path) -> None:
    """Once fork-e2e/pr-N exists, a first-timer's new push re-mirrors.

    The accepted always-re-mirror behavior: a first-timer with no approval
    whose branch already exists still mirrors (new commits re-run e2e).
    Asserts ``mirror=true`` despite the otherwise-closed gate.
    """
    out = _run(
        tmp_path,
        author_association="FIRST_TIME_CONTRIBUTOR",
        branch_exists=True,
    )
    assert out["mirror"] == "true"
    assert "re-mirror" in out["reason"]
