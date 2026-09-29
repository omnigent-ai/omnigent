"""Exercise Polly's required database reference when composing the review prompt."""

import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/polly-review.yml"
_BOOTSTRAP_ENDPOINT = (
    "repos/omnigent-ai/omnigent/contents/docs/DATABASE_BEST_PRACTICES.md"
    "?ref=2a3657c2e8081c37cc6741fe062c0ac50a02934f"
)
_BOOTSTRAP_SHA256 = "a8c4c1237aad86004f47475f81a3fb8bde672e095fa4e6f87ad79c89a5b381c4"
_TEST_BOOTSTRAP_GUIDANCE = b"# Database practices\n\nKeep application writes bounded.\n"
_TEST_BOOTSTRAP_SHA256 = hashlib.sha256(_TEST_BOOTSTRAP_GUIDANCE).hexdigest()


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
    (artifacts / "gh_response").write_bytes(b"Unexpected fetch")
    (artifacts / "gh_exit_code").write_text("99")
    bin_directory = checkout / "bin"
    bin_directory.mkdir()
    gh = bin_directory / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "artifacts = pathlib.Path('artifacts')\n"
        "(artifacts / 'gh_args.json').write_text(json.dumps(sys.argv[1:]))\n"
        "sys.stdout.buffer.write((artifacts / 'gh_response').read_bytes())\n"
        "sys.exit(int((artifacts / 'gh_exit_code').read_text()))\n"
    )
    gh.chmod(0o755)
    return checkout


def _generate_prompt(checkout: Path, author_association: str) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("id") == "ctx")
    script = step["run"].split("python3 -u <<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    artifacts = checkout / "artifacts"
    script = script.replace("/tmp/", str(artifacts) + "/")
    # Exercise verification with fixed fixture bytes, independent of the evolving guide.
    assert script.count(_BOOTSTRAP_SHA256) == 1
    script = script.replace(_BOOTSTRAP_SHA256, _TEST_BOOTSTRAP_SHA256)
    (artifacts / "pr_author_assoc.txt").write_text(author_association)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=checkout,
        env={"PATH": str(checkout / "bin")},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def _prompt_references(checkout: Path) -> set[Path]:
    prompt = (checkout / "artifacts/review_prompt.txt").read_text()
    return {
        Path(path)
        for path in re.findall(r"`([^`\n]*best_practices\.md)`", prompt, flags=re.IGNORECASE)
    }


def _expect_bootstrap_fetch(checkout: Path) -> None:
    assert json.loads((checkout / "artifacts/gh_args.json").read_text()) == [
        "api",
        _BOOTSTRAP_ENDPOINT,
        "-H",
        "Accept: application/vnd.github.raw+json",
    ]


@pytest.mark.parametrize("author_association", ["MEMBER", "NONE"], ids=["internal", "external"])
def test_prompt_references_readable_guidance_from_checkout(
    prompt_workspace: Path, author_association: str
) -> None:
    guidance = prompt_workspace / "docs/DATABASE_BEST_PRACTICES.md"
    contents = "# Database practices\n\nKeep columns small — review byte limits.\n"
    guidance.write_text(contents, encoding="utf-8")

    result = _generate_prompt(prompt_workspace, author_association)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (prompt_workspace / "artifacts/gh_args.json").exists()
    references = _prompt_references(prompt_workspace)
    assert references == {guidance.resolve()}
    reference = references.pop()
    assert reference.is_absolute()
    assert reference.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("failure", ["blank", "directory", "invalid-utf8"])
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
    assert not (prompt_workspace / "artifacts/gh_args.json").exists()
    assert not (prompt_workspace / "artifacts/polly_database_best_practices.md").exists()
    assert not (prompt_workspace / "artifacts/review_prompt.txt").exists()


@pytest.mark.parametrize("author_association", ["MEMBER", "NONE"], ids=["internal", "external"])
def test_missing_local_guidance_uses_verified_bootstrap_snapshot(
    prompt_workspace: Path, author_association: str
) -> None:
    artifacts = prompt_workspace / "artifacts"
    (artifacts / "gh_response").write_bytes(_TEST_BOOTSTRAP_GUIDANCE)
    (artifacts / "gh_exit_code").write_text("0")

    result = _generate_prompt(prompt_workspace, author_association)

    assert result.returncode == 0, result.stdout + result.stderr
    _expect_bootstrap_fetch(prompt_workspace)
    snapshot = artifacts / "polly_database_best_practices.md"
    assert _prompt_references(prompt_workspace) == {snapshot.resolve()}
    assert snapshot.read_bytes() == _TEST_BOOTSTRAP_GUIDANCE
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == _TEST_BOOTSTRAP_SHA256
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o444


@pytest.mark.parametrize("failure", ["tampered", "nonzero-exit"])
def test_bootstrap_failure_does_not_publish_guidance_or_prompt(
    prompt_workspace: Path, failure: str
) -> None:
    artifacts = prompt_workspace / "artifacts"
    response = (
        _TEST_BOOTSTRAP_GUIDANCE + b"\nTampered.\n"
        if failure == "tampered"
        else _TEST_BOOTSTRAP_GUIDANCE
    )
    (artifacts / "gh_response").write_bytes(response)
    (artifacts / "gh_exit_code").write_text("0" if failure == "tampered" else "1")

    result = _generate_prompt(prompt_workspace, "MEMBER")

    assert result.returncode != 0
    assert "::error::" in result.stderr
    _expect_bootstrap_fetch(prompt_workspace)
    assert not (artifacts / "polly_database_best_practices.md").exists()
    assert not (prompt_workspace / "artifacts/review_prompt.txt").exists()
