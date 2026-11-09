"""Reference-policy matching and staged/branch diff coverage."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dev.lint import lint_reference_policy as policy
from dev.lint.lint_reference_policy import check, git, violates


@pytest.fixture
def sample_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        policy, "PROHIBITED_NAMES", frozenset({"sampletool", "sampletools", "demotool"})
    )
    monkeypatch.setattr(policy, "PHRASES", re.compile(r"example[\s_-]*restricted", re.IGNORECASE))
    monkeypatch.setattr(
        policy, "HOST", policy.host_pattern(("service.example.test", "apps.example.test"))
    )
    monkeypatch.setattr(policy, "PUBLIC_HOSTS", frozenset({"docs.service.example.test"}))


@pytest.mark.parametrize(
    "text",
    [
        "Sampletool",
        "sampletools",
        "DEMOTOOL",
        "refreshSampletool",
        "SAMPLETOOL_HOST",
        "useSampletoolHost",
        "exampleRestricted",
        "Example-restricted",
        "example restricted",
        "https://tenant.service.example.test/path",
        "https://tenant.apps.example.test/path",
        "EXAMPLE.SERVICE.EXAMPLE.TEST",
        "123.service.example.test",
        "docs.extra.service.example.test",
    ],
)
@pytest.mark.usefixtures("sample_policy")
def test_prohibited(text: str) -> None:
    assert violates(text)


@pytest.mark.parametrize(
    "text",
    [
        "varchar",
        "sampletoolbox",
        "xsampletool",
        "internal server error",
        "https://docs.service.example.test/en/",
        "https://DOCS.SERVICE.EXAMPLE.TEST/en/",
        "https://tenant.example.test/path",
        "https://service.example.test/",
        "https://tenant.service.example.test.other.example/",
    ],
)
@pytest.mark.usefixtures("sample_policy")
def test_allowed(text: str) -> None:
    assert not violates(text)


def test_configured_names_are_prohibited() -> None:
    assert policy.PROHIBITED_NAMES
    for name in policy.PROHIBITED_NAMES:
        assert violates(name)
        assert violates(name.upper())
        assert violates(f"before_{name}_after")
        assert violates(f"use{name.title()}Value")


@pytest.mark.parametrize("label", ["example", "test", "workspace", "123", "12345678"])
def test_sample_labels_do_not_exempt_policy_hosts(label: str) -> None:
    for suffix in policy.HOST_SUFFIXES:
        assert violates(f"https://{label}.{suffix}/path")


def test_public_hosts_and_reserved_examples_are_allowed() -> None:
    for host in policy.PUBLIC_HOSTS:
        assert not violates(f"https://{host}/path")
        assert violates(f"extra.{host}")
    for text in ["Databricks OAuth", "https://workspace.example.test/", "https://example.com/"]:
        assert not violates(text)


def test_fixture_source_follows_policy() -> None:
    assert not violates(Path(__file__).read_text(encoding="utf-8"))


@pytest.fixture
def prohibited_name() -> str:
    return min(policy.PROHIBITED_NAMES)


@pytest.fixture
def repository(tmp_path: Path, prohibited_name: str) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "existing.txt").write_text(f"{prohibited_name}\nkeep\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "initial")
    return tmp_path


def test_staged_only_and_existing_lines(repository: Path, prohibited_name: str) -> None:
    path = repository / "existing.txt"
    path.write_text(f"{prohibited_name}\nkeep\nsafe\n", encoding="utf-8")
    git(repository, "add", ".")
    path.write_text(f"{prohibited_name}\nkeep\n{prohibited_name}\n", encoding="utf-8")
    assert check(repository) == []
    git(repository, "add", ".")
    assert check(repository) == ["existing.txt:3: prohibited reference in added text"]


def test_branch_range_checks_committed_changes(repository: Path, prohibited_name: str) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "notes.md").write_text(f"# Note\n{prohibited_name}\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "change")
    assert check(repository) == []
    assert check(repository, base, "HEAD") == ["notes.md:2: prohibited reference in added text"]


def test_paths_renames_and_line_numbers(repository: Path, prohibited_name: str) -> None:
    name = f"{prohibited_name} notes.txt"
    (repository / name).write_text("safe\n", encoding="utf-8")
    git(repository, "mv", "existing.txt", "renamed.txt")
    git(repository, "add", ".")
    assert sorted(check(repository)) == sorted(
        [
            f"{name}:1: prohibited reference in new path",
            "renamed.txt:1: prohibited reference in added text",
        ]
    )


def test_deletion_and_binary_content(repository: Path) -> None:
    git(repository, "rm", "existing.txt")
    (repository / "image.bin").write_bytes(b"\x00\xff")
    git(repository, "add", ".")
    assert check(repository) == []


def test_only_policy_definition_is_exempt(repository: Path, prohibited_name: str) -> None:
    assert {"dev/lint/lint_reference_policy.py"} == policy.POLICY_FILES
    for name in [
        *policy.POLICY_FILES,
        "docs/policy.md",
        "tests/dev/lint/test_lint_reference_policy.py",
        "tests/other.py",
    ]:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{prohibited_name}\n", encoding="utf-8")
    git(repository, "add", ".")
    assert check(repository) == [
        "docs/policy.md:1: prohibited reference in added text",
        "tests/dev/lint/test_lint_reference_policy.py:1: prohibited reference in added text",
        "tests/other.py:1: prohibited reference in added text",
    ]


def test_missing_base_fails_closed(repository: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        check(repository, "missing-ref", "HEAD")


def test_cli_uses_push_range_and_does_not_echo_values(
    repository: Path, prohibited_name: str
) -> None:
    script = Path(__file__).resolve().parents[3] / "dev/lint/lint_reference_policy.py"
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "notes.txt").write_text(f"{prohibited_name}\n", encoding="utf-8")
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
    assert prohibited_name not in result.stdout
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
