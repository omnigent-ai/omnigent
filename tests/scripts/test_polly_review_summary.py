"""Validate frozen-diff metrics and the summary publication gate without an LLM."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / ".github/scripts/polly-review-summary.py"
_HELPERS = runpy.run_path(str(_SCRIPT))
_REVIEW = """## Blocking issues
None.
## Summary
### Changes
Fix the parser.
### Tests
| Test / case | Behavior protected | Layer | Needed? | Action / rationale |
| --- | --- | --- | --- | --- |
| tests/test_parser.py::test_empty | Empty input | Unit | Keep | Guards the regression. |
### Scope
All changes support the parser fix.
"""


def test_metrics_use_real_patch_including_binary_rename_and_quoted_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)

    git("-c", "init.templateDir=", "init")
    files = {
        "app.py": b"old\n",
        "tests/test_app.py": b"old\n",
        "tests/test caf\u00e9\tname.py": b"old\n",
        "docs/note.md": b"old\n",
        "pnpm-lock.yaml": b"old\n",
        "asset.bin": b"\x00old",
        "rename.py": b"unchanged\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git("add", ".")
    for name in files:
        if name == "rename.py":
            (repo / name).rename(repo / "renamed.py")
            git("add", "-N", "renamed.py")
        else:
            content = b"new\n"
            if name == "app.py":
                content = b"new\nmore\n"
            elif name == "tests/test_app.py":
                content = b"new\ncase2\ncase3\n"
            elif name == "asset.bin":
                content = b"\x00new"
            (repo / name).write_bytes(content)
    patch = tmp_path / "review.diff"
    patch.write_bytes(git("diff", "--binary", "--find-renames"))

    stats = _HELPERS["diff_stats"](patch)

    assert "7 files; +8 / -5 lines (13 changed text lines); 1 binary files" in stats["markdown"]
    assert "| Tests and test support | 2 | 4 | 2 | 46.2% |" in stats["markdown"]
    assert "| Documentation | 1 | 1 | 1 | 15.4% |" in stats["markdown"]
    assert "| Dependencies and lockfiles | 1 | 1 | 1 | 15.4% |" in stats["markdown"]
    assert set(stats["test_files"]) == {"tests/test_app.py", "tests/test caf\u00e9\tname.py"}


@pytest.mark.parametrize(
    "path", ["web/src/View.test.tsx", "web/electron/e2e/a.js", "tests/helpers.py"]
)
def test_colocated_browser_and_support_files_count_as_tests(path: str) -> None:
    assert _HELPERS["category"](path) == "Tests and test support"


def test_empty_diff_has_no_percentage_or_test_inventory(tmp_path: Path) -> None:
    patch = tmp_path / "empty.diff"
    patch.write_text("")
    stats = _HELPERS["diff_stats"](patch)
    assert "0 files; +0 / -0 lines" in stats["markdown"]
    assert "| Tests and test support | 0 | 0 | 0 | N/A |" in stats["markdown"]
    assert stats["test_files"] == []


def test_invalid_diff_fails_instead_of_claiming_zero_changes(tmp_path: Path) -> None:
    patch = tmp_path / "invalid.diff"
    patch.write_text("Not a diff\n")
    with pytest.raises(subprocess.CalledProcessError):
        _HELPERS["diff_stats"](patch)


@pytest.mark.parametrize(
    "review",
    [
        _REVIEW.replace("## Summary", "## Conclusion"),
        _REVIEW + "\n## Summary\nDuplicate\n",
        _REVIEW.replace("### Changes", "### Other"),
        _REVIEW.replace("Fix the parser.", ""),
        _REVIEW.replace("### Tests", "### Other"),
        _REVIEW.replace("tests/test_parser.py", "tests/test_unrelated.py"),
        _REVIEW.replace("| Keep |", "| |"),
        _REVIEW.replace("Guards the regression.", ""),
        _REVIEW.replace("| Keep |", "| Yes |"),
        _REVIEW.replace("| Test / case |", "| File |"),
        _REVIEW.replace("tests/test_parser.py::test_empty", "another_test")
        + "\nMention only: tests/test_parser.py\n",
        _REVIEW.replace("### Scope", "### Other"),
        _REVIEW + "x" * 60000,
    ],
    ids=[
        "missing-summary",
        "duplicate-summary",
        "missing-changes",
        "empty-changes",
        "missing-tests",
        "unassessed-file",
        "missing-verdict",
        "missing-rationale",
        "unknown-verdict",
        "missing-table-header",
        "mentioned-without-assessment",
        "missing-scope",
        "oversized",
    ],
)
def test_incomplete_or_oversized_reviews_are_rejected(review: str) -> None:
    with pytest.raises(ValueError):
        _HELPERS["compose_review"](
            review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )


def test_multi_file_review_rejects_missing_path_even_if_mentioned_in_prose() -> None:
    review = (
        _REVIEW.replace("tests/test_parser.py::test_empty", "test_empty")
        .replace("### Tests\n", "### Tests\nThe tests are in tests/test_parser.py.\n")
        .replace(
            "### Scope\n",
            "| tests/test_other.py::test_other | Other input | Unit | keep | Needed. |\n"
            "### Scope\n",
        )
    )

    with pytest.raises(ValueError, match=r"unassessed test files: tests/test_parser\.py"):
        _HELPERS["compose_review"](
            review,
            {
                "markdown": "Computed counts",
                "test_files": ["tests/test_parser.py", "tests/test_other.py"],
            },
        )


@pytest.mark.parametrize("case", ["test_empty", "test_route[/v1/items]"])
@pytest.mark.parametrize("reference", ["`tests/test_parser.py`", "tests/test_parser.py"])
def test_single_changed_test_file_accepts_case_only_rows(case: str, reference: str) -> None:
    review = _REVIEW.replace("tests/test_parser.py::test_empty", f"`{case}`").replace(
        "### Tests\n", f"### Tests\nAll changed tests are in {reference}.\n"
    )

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )

    assert f"`{case}`" in result
    assert "Test-by-test assessment" in result


@pytest.mark.parametrize(
    "case",
    [
        "test_route[/v1/items]",
        "**test_route[/v1/items]**",
        "test_a, test_b",
        "test_route[/v1/items], test_route[/v2/items]",
    ],
)
def test_single_file_accepts_plain_parameterized_case_labels(case: str) -> None:
    review = _REVIEW.replace("tests/test_parser.py::test_empty", case).replace(
        "### Tests\n", "### Tests\nChanged file: tests/test_parser.py.\n"
    )

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )
    assert case in result


def test_single_file_accepts_case_row_with_an_example_path() -> None:
    review = _REVIEW.replace(
        "tests/test_parser.py::test_empty",
        "`test_case` — `tests/test_unrelated.py::test_other`",
    ).replace("### Tests\n", "### Tests\nChanged file: `tests/test_parser.py`.\n")

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )
    assert "`test_case` — `tests/test_unrelated.py::test_other`" in result


@pytest.mark.parametrize(
    "other_case",
    [
        "tests/test_unrelated.py::test_other",
        "`tests/test_unrelated.py::test_other` — example",
        "[tests/test_unrelated.py::test_other](#test)",
        "See tests/test_unrelated.py::test_other",
    ],
)
def test_single_changed_test_file_rejects_row_for_another_file(other_case: str) -> None:
    review = _REVIEW.replace("tests/test_parser.py::test_empty", other_case).replace(
        "### Tests\n", "### Tests\nChanged file: `tests/test_parser.py`.\n"
    )

    with pytest.raises(ValueError, match=r"unassessed test files: tests/test_parser\.py"):
        _HELPERS["compose_review"](
            review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )


@pytest.mark.parametrize(
    "reference",
    [
        "tests/test_parser.py.backup",
        "archive/tests/test_parser.py",
        "tests/test_parser.py-old",
        "tests/test_parser.py~",
        "tests/test_parser.py+old",
        "~tests/test_parser.py",
    ],
)
def test_single_file_requires_exact_path_in_tests(reference: str) -> None:
    review = _REVIEW.replace("tests/test_parser.py::test_empty", "test_route[/v1/items]").replace(
        "### Tests\n", f"### Tests\nChanged file: `{reference}`.\n"
    )

    with pytest.raises(ValueError, match=r"unassessed test files: tests/test_parser\.py"):
        _HELPERS["compose_review"](
            review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )


@pytest.mark.parametrize("label", ["**", "` `"])
def test_single_file_rejects_empty_case_label(label: str) -> None:
    review = _REVIEW.replace("tests/test_parser.py::test_empty", label).replace(
        "### Tests\n", "### Tests\nChanged file: `tests/test_parser.py`.\n"
    )

    with pytest.raises(ValueError, match=r"unassessed test files: tests/test_parser\.py"):
        _HELPERS["compose_review"](
            review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )


def test_single_changed_test_file_still_requires_assessment_rows() -> None:
    review = _REVIEW.replace(
        "| tests/test_parser.py::test_empty | Empty input | Unit | Keep |"
        " Guards the regression. |",
        "",
    ).replace("### Tests\n", "### Tests\nAll changed tests are in `tests/test_parser.py`.\n")

    with pytest.raises(ValueError, match="unassessed test files"):
        _HELPERS["compose_review"](
            review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )


def test_case_only_rows_do_not_cover_multiple_changed_test_files() -> None:
    review = _REVIEW.replace("tests/test_parser.py::test_empty", "test_empty")

    with pytest.raises(ValueError, match="unassessed test files"):
        _HELPERS["compose_review"](
            review,
            {
                "markdown": "Computed counts",
                "test_files": ["tests/test_parser.py", "tests/test_other.py"],
            },
        )


def test_workflow_inserts_computed_summary_before_secret_scan_and_publication(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/polly-review.yml").read_text())
    steps = workflow["jobs"]["review"]["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Build review summary") < names.index(
        "Scan review output for secrets before posting"
    )
    assert names.index("Scan review output for secrets before posting") < names.index(
        "Post review comment"
    )
    stats = {
        "markdown": "**Diff size (computed):** fixture counts",
        "test_files": ["tests/test_parser.py"],
    }
    (tmp_path / "pr_diff_summary.json").write_text(json.dumps(stats))
    review = tmp_path / "polly_review.txt"
    review.write_text(_REVIEW)
    (tmp_path / "python3").symlink_to(sys.executable)
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    for name in ("Build review summary", "Post review comment"):
        step = next(step for step in steps if step["name"] == name)
        script = step["run"].replace(".github/scripts/polly-review-summary.py", str(_SCRIPT))
        script = script.replace("/tmp/", str(tmp_path) + "/")
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
                "HEAD_SHA": "abc",
                "RUN_URL": "https://example.test/run",
                "GITHUB_RUN_ID": "10",
                "GITHUB_RUN_ATTEMPT": "1",
                "PR_NUMBER": "1",
                "REPO": "test/repo",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    expected = _REVIEW.replace("## Summary\n", f"## Summary\n\n{stats['markdown']}\n\n")
    expected = expected.replace(
        "### Tests\n", "### Tests\n\n<details>\n<summary>Test-by-test assessment</summary>\n\n"
    ).replace("### Scope\n", "\n</details>\n\n### Scope\n")
    assert review.read_text() == expected
    assert expected in (tmp_path / "comment.md").read_text()

    assert (tmp_path / "polly-completed-sha.txt").read_text().strip() == "abc"
    receipt = next(step for step in steps if step["name"] == "Upload Polly completion receipt")
    assert (
        receipt["if"]
        == "steps.publish.outcome == 'success' && github.event_name != 'pull_request'"
    )
    publish = next(step for step in steps if step["name"] == "Post review comment")
    assert publish["id"] == "publish"
    assert steps.index(publish) < steps.index(receipt)
    assert receipt["with"]["path"] == "/tmp/polly-completed-sha.txt"
    assert receipt["with"]["overwrite"] is True
    assert receipt["with"]["name"] == (
        "polly-completed-${{ steps.pr.outputs.pr_number }}-${{ steps.ctx.outputs.head_sha }}"
    )
