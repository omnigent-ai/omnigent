from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/fork-e2e/should-mirror.sh"


def _run(
    tmp_path: Path,
    *,
    labels: str = "",
    labeler: str = "",
    maintainers: str = "alice bob",
) -> dict[str, str]:
    """
    Run should-mirror.sh against a mocked ``gh`` and return its outputs.

    The label-only gate makes exactly two ``gh`` calls, which the mock answers:

    - ``pr view {pr} ... --json labels --jq '.labels[].name'`` -> *labels*, a
      space-separated label list printed one per line (the post-``--jq`` shape
      the script greps).
    - ``api repos/{repo}/issues/{pr}/events ...`` -> *labeler*, the login of the
      account that last applied the gate label (empty if none).

    :param tmp_path: Pytest tmp dir for the mock + output file.
    :param labels: Space-separated labels currently on the PR; empty means none.
    :param labeler: Login the issue-events mock attributes the gate label to.
    :param maintainers: Space-separated maintainer logins (as
        load-maintainers.sh would emit); empty means none.
    :returns: Parsed ``key=value`` GITHUB_OUTPUT lines, e.g.
        ``{"mirror": "true", "reason": "..."}``.
    """
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        # gh pr view <pr> --repo <repo> --json labels --jq '.labels[].name'
        # shellcheck-style: unquoted expansion intentionally splits labels.
        'if [[ "$1" == "pr" ]]; then [[ -n "$MOCK_LABELS" ]]'
        ' && printf "%s\\n" $MOCK_LABELS; exit 0; fi\n'
        'if [[ "$1" == "api" ]]; then\n'
        '  case "$2" in\n'
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
            "LABEL": "e2e-approved",
            "MIRROR_BRANCH": "fork-e2e/pr-7",
            "MAINTAINERS": maintainers,
            "GITHUB_OUTPUT": str(out_file),
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


def test_label_applied_by_maintainer_mirrors(tmp_path: Path) -> None:
    """The gate label, applied by a maintainer, opens the gate.

    The whole contract: secret-bearing e2e runs only after a maintainer applies
    ``e2e-approved``. Asserts ``mirror=true`` and that the reason names the
    maintainer who applied it.
    """
    out = _run(tmp_path, labels="e2e-approved", labeler="bob")
    assert out["mirror"] == "true"
    assert "applied by maintainer" in out["reason"]


def test_maintainer_match_is_case_insensitive(tmp_path: Path) -> None:
    """Labeler vs MAINTAINER comparison is case-insensitive.

    GitHub logins are compared lowercased, so a maintainer labelled as ``Bob``
    still opens the gate against a ``bob`` MAINTAINER entry.
    """
    out = _run(tmp_path, labels="e2e-approved", labeler="Bob", maintainers="alice bob")
    assert out["mirror"] == "true"


def test_label_absent_does_not_mirror(tmp_path: Path) -> None:
    """Without the gate label the gate stays shut.

    No ``e2e-approved`` means no secret-bearing e2e on a fork PR. Asserts
    ``mirror=false`` and an awaiting-label reason.
    """
    out = _run(tmp_path, labels="", labeler="bob")
    assert out["mirror"] == "false"
    assert "awaiting" in out["reason"]


def test_other_labels_are_ignored(tmp_path: Path) -> None:
    """Unrelated labels never open the gate.

    Only the exact gate label counts; ``bug``/``enhancement`` leave it shut.
    """
    out = _run(tmp_path, labels="bug enhancement", labeler="bob")
    assert out["mirror"] == "false"
    assert "awaiting" in out["reason"]


def test_label_applied_by_non_maintainer_does_not_mirror(tmp_path: Path) -> None:
    """The gate label applied by a NON-maintainer must not open the gate.

    Triage+ access lets non-maintainers apply labels too, so label presence
    alone is insufficient: the labeler must be in MAINTAINER. ``eve`` applies it
    but isn't a maintainer, so ``mirror=false``.
    """
    out = _run(tmp_path, labels="e2e-approved", labeler="eve", maintainers="alice bob")
    assert out["mirror"] == "false"
    assert "non-maintainer" in out["reason"]


def test_label_present_but_unattributable_does_not_mirror(tmp_path: Path) -> None:
    """A present label with no labeled-event actor stays shut (fail closed).

    If the label can't be attributed to anyone (e.g. seeded outside the events
    timeline), we can't confirm a maintainer applied it, so ``mirror=false``.
    """
    out = _run(tmp_path, labels="e2e-approved", labeler="")
    assert out["mirror"] == "false"
    assert "no attributable labeler" in out["reason"]


def test_no_maintainers_loaded_does_not_mirror(tmp_path: Path) -> None:
    """An empty MAINTAINER list fails closed.

    With no maintainers to verify against, even a present label can't be
    trusted, so the gate stays shut regardless of the labeler.
    """
    out = _run(tmp_path, labels="e2e-approved", labeler="bob", maintainers="")
    assert out["mirror"] == "false"
    assert "no maintainers" in out["reason"]
