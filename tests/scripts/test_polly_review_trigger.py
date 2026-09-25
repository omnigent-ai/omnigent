"""Run Polly's command validation with real comment bodies in a shell."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/polly-review.yml"


@pytest.mark.parametrize(
    ("body", "eligible"),
    [
        pytest.param("/review", True, id="command"),
        pytest.param("  \t/review  ", True, id="leading-whitespace"),
        pytest.param("/review force", True, id="force"),
        pytest.param("Please check this.\n  /review force\nThanks!", True, id="later-line"),
        pytest.param("Please check this.\r\n/review\r\nThanks!", True, id="crlf"),
        pytest.param("/review\tforce", True, id="tab-separated"),
        pytest.param("Try /review later.", False, id="inline-mention"),
        pytest.param("Try `/review` later.", False, id="inline-code"),
        pytest.param(
            "## Polly AI Review\nUse `/review force` to review again.", False, id="bot-footer"
        ),
        pytest.param("> /review", False, id="quoted-command"),
        pytest.param("/reviewer", False, id="different-command"),
        pytest.param("/review-force", False, id="no-word-boundary"),
        pytest.param("/REVIEW", False, id="case-sensitive"),
        pytest.param("", False, id="empty"),
        pytest.param(" \n\t", False, id="whitespace"),
        pytest.param("$(touch injected) /review", False, id="shell-substitution"),
    ],
)
def test_comment_command_eligibility(tmp_path: Path, body: str, eligible: bool) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["trigger"]["steps"] if s.get("id") == "command")
    output = tmp_path / "github_output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": os.defpath, "COMMENT_BODY": body, "GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"eligible={str(eligible).lower()}\n"
    assert not (tmp_path / "injected").exists()
