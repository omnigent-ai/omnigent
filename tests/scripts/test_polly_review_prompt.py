"""Exercise Polly's required database reference when composing the review prompt."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/polly-review.yml"


@pytest.fixture
def prompt_workspace(tmp_path: Path) -> Path:
    checkout = tmp_path / "trusted checkout"
    (checkout / "docs").mkdir(parents=True)
    artifacts = checkout / "artifacts"
    artifacts.mkdir()
    (artifacts / "pr_meta.json").write_text(
        json.dumps(
            {
                "title": "Add an application table",
                "body": "The schema supports the new feature.",
                "baseRefName": "main",
                "headRefName": "feature",
                "baseRefOid": "a" * 40,
                "headRefOid": "b" * 40,
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
        )
    )
    (artifacts / "pr_diff.txt").write_text("+CREATE TABLE example (id INTEGER);\n")
    (artifacts / "lockfile_pins.txt").write_text("")
    return checkout


def _generate_prompt(checkout: Path, author_association: str) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("id") == "ctx")
    script = step["run"].split("python3 -u <<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    artifacts = checkout / "artifacts"
    script = script.replace("/tmp/", str(artifacts) + "/")
    (artifacts / "pr_author_assoc.txt").write_text(author_association)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=checkout,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("author_association", ["MEMBER", "NONE"], ids=["internal", "external"])
def test_prompt_references_readable_guidance_from_checkout(
    prompt_workspace: Path, author_association: str
) -> None:
    guidance = prompt_workspace / "docs/DATABASE_BEST_PRACTICES.md"
    contents = "# Database practices\n\nKeep columns small — review byte limits.\n"
    guidance.write_text(contents, encoding="utf-8")

    result = _generate_prompt(prompt_workspace, author_association)

    assert result.returncode == 0, result.stdout + result.stderr
    prompt = (prompt_workspace / "artifacts/review_prompt.txt").read_text()
    references = {
        Path(path) for path in re.findall(r"`([^`\n]*DATABASE_BEST_PRACTICES\.md)`", prompt)
    }
    assert references == {guidance.resolve()}
    reference = references.pop()
    assert reference.is_absolute()
    assert reference.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("failure", ["missing", "blank", "directory", "invalid-utf8"])
def test_prompt_generation_fails_without_usable_guidance(
    prompt_workspace: Path, failure: str
) -> None:
    guidance = prompt_workspace / "docs/DATABASE_BEST_PRACTICES.md"
    if failure == "blank":
        guidance.write_text(" \n\t")
    elif failure == "directory":
        guidance.mkdir()
    elif failure == "invalid-utf8":
        guidance.write_bytes(b"# Database practices\n\xff")

    result = _generate_prompt(prompt_workspace, "MEMBER")

    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert str(guidance.resolve()) in result.stderr
    assert not (prompt_workspace / "artifacts/review_prompt.txt").exists()
