"""Reference-policy matching and staged/branch diff coverage."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dev.lint.lint_reference_policy import POLICY_FILES, check, git, violates


@pytest.mark.parametrize(
    "text",
    [
        "Arca",
        "arclets",
        "ISAAC",
        "refreshDbcert",
        "ARCA_HOST",
        "useArcaHost",
        "lakebox",
        "ucode",
        "dbexec",
        "SPOG",
        "databricksInternalFeaturesEnabled",
        "Databricks-internal",
        "--internal-beta",
        "internal Databricks",
        "Databricks corporate network",
        "https://github.com/databricks-eng/project",
        "e2-dogfood",
        "https://dbc-deadbeef-abcd.cloud.databricks.com/path",
        "123987.ai-gateway.cloud.databricks.com",
        "adb-987654.12.azuredatabricks.net",
        "private.gcp.databricks.com",
        "private.databricksapps.com",
        "package-mirror.cloud.databricks.com",
        "example.evil.cloud.databricks.com",
    ],
)
def test_prohibited(text: str) -> None:
    assert violates(text)


@pytest.mark.parametrize(
    "text",
    [
        "Databricks OAuth",
        "varchar",
        "arcade",
        "internal server error",
        "https://docs.databricks.com/en/",
        "https://accounts.cloud.databricks.com/",
        "https://example.cloud.databricks.com/omnigent",
        "https://example.staging.cloud.databricks.com",
        "https://dbc-12345678-abcd.cloud.databricks.com",
        "https://4242424242424242.ai-gateway.cloud.databricks.com",
        "https://example.azuredatabricks.net",
        "https://app.example.databricksapps.com",
    ],
)
def test_allowed(text: str) -> None:
    assert not violates(text)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "existing.txt").write_text("arca\nkeep\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "initial")
    return tmp_path


def test_staged_only_and_existing_lines(repository: Path) -> None:
    path = repository / "existing.txt"
    path.write_text("arca\nkeep\nsafe\n", encoding="utf-8")
    git(repository, "add", ".")
    path.write_text("arca\nkeep\nisaac\n", encoding="utf-8")
    assert check(repository) == []
    git(repository, "add", ".")
    assert check(repository) == ["existing.txt:3: prohibited reference in added text"]


def test_branch_range_checks_committed_changes(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "notes.md").write_text("# Note\narclets\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "change")
    assert check(repository) == []
    assert check(repository, base, "HEAD") == ["notes.md:2: prohibited reference in added text"]


def test_paths_renames_and_line_numbers(repository: Path) -> None:
    (repository / "isaac notes.txt").write_text("safe\n", encoding="utf-8")
    git(repository, "mv", "existing.txt", "renamed.txt")
    git(repository, "add", ".")
    assert check(repository) == [
        "isaac notes.txt:1: prohibited reference in new path",
        "renamed.txt:1: prohibited reference in added text",
    ]


def test_deletion_and_binary_content(repository: Path) -> None:
    git(repository, "rm", "existing.txt")
    (repository / "image.bin").write_bytes(b"\x00\xff")
    git(repository, "add", ".")
    assert check(repository) == []


def test_only_policy_and_its_tests_are_exempt(repository: Path) -> None:
    for name in [*POLICY_FILES, "docs/policy.md", "tests/other.py"]:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("arca\n", encoding="utf-8")
    git(repository, "add", ".")
    assert check(repository) == [
        "docs/policy.md:1: prohibited reference in added text",
        "tests/other.py:1: prohibited reference in added text",
    ]


def test_missing_base_fails_closed(repository: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        check(repository, "missing-ref", "HEAD")


def test_cli_uses_push_range_and_does_not_echo_values(repository: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "dev/lint/lint_reference_policy.py"
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "notes.txt").write_text("isaac\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "change")
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=repository,
        env={**os.environ, "PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": "HEAD"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "notes.txt:1: prohibited reference" in result.stdout
    assert "isaac" not in result.stdout
    failed = subprocess.run(
        [sys.executable, str(script), "--base", "missing-ref", "--head", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 2


def test_hook_and_ci_enforce_policy() -> None:
    root = Path(__file__).resolve().parents[3]
    hook = (root / ".pre-commit-config.yaml").read_text()
    workflow = (root / ".github/workflows/lint.yml").read_text()
    prompt = (root / ".github/workflows/polly-review.yml").read_text()
    assert "entry: python3 dev/lint/lint_reference_policy.py" in hook
    assert 'lint_reference_policy.py --base "$POLICY_BASE" --head HEAD' in workflow
    assert "fetch-depth: 0" in workflow
    assert "**Reference policy** — treat new violations as blocking contract violations." in prompt
    assert "Do not explain prohibited names" in prompt
