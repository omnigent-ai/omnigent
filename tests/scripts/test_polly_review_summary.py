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
        _REVIEW.replace("tests/test_parser.py", "tests/test_unrelated.py") + "x" * 70000,
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
        "oversized-unassessed-file",
    ],
)
def test_incomplete_reviews_are_rejected_regardless_of_size(review: str) -> None:
    with pytest.raises(ValueError):
        _HELPERS["compose_review"](
            review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )


@pytest.mark.parametrize(
    (
        "rationale_specs",
        "preceding_paragraph_length",
        "row_indentation",
        "inline_details",
        "run_path",
        "oversized_field",
        "terminal_remainder",
        "adjacent_tables",
        "oversized_nonassessment",
        "nonprogress",
        "final_table_newline",
    ),
    [
        (None, 0, "", False, "run", None, None, False, False, False, None),
        ((20000, 45000, 20000), 0, "", False, "run", None, None, False, False, False, None),
        ((3000,) * 30, 59194, "", False, "run", None, None, False, False, False, None),
        (None, 0, "  ", False, "run", None, None, False, False, False, None),
        ((70000,), 0, "", False, "run", None, None, False, False, False, None),
        # Align an escaped pipe with the fully wrapped comment boundary.
        (
            ("y" * 69 + ("x" * 70 + r"\|") * 1100,),
            0,
            "",
            False,
            "r" * 66,
            None,
            None,
            False,
            False,
            False,
            None,
        ),
        (None, 0, "", True, "run", None, None, False, False, False, None),
        (("Reason",), 0, "", False, "run", 0, None, False, False, False, None),
        (("Reason",), 0, "", False, "run", 1, None, False, False, False, None),
        (("Reason",), 0, "", False, "run", 2, None, False, False, False, None),
        ((177655, "Tail."), 0, "", False, "run", None, " | \t", False, False, False, None),
        ((177654, "Tail."), 0, "", False, "run", None, "| \t", False, False, False, None),
        ((177653, "Tail."), 0, "", False, "run", None, " \t", False, False, False, None),
        ((177652, "Tail."), 0, "", False, "run", None, "\t", False, False, False, None),
        ((177651, "Tail."), 0, "", False, "run", None, "", False, False, False, None),
        (
            ("Reason." + " " * 135000 + "Tail.",),
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            False,
            None,
        ),
        (None, 0, "", False, "run", None, None, True, False, False, None),
        (("Reason",), 0, "", False, "run", None, None, False, True, False, None),
        (("Reason",), 0, "", False, "run", None, None, False, False, True, None),
        (None, 0, "", False, "run", None, None, False, False, False, ""),
        (None, 0, "", False, "run", None, None, False, False, False, "\n"),
    ],
    ids=[
        "many-rows",
        "unequal-rows",
        "header-boundary",
        "indented-rows",
        "oversized-row",
        "escaped-pipe-boundary",
        "inline-details",
        "oversized-test",
        "oversized-behavior",
        "oversized-layer",
        "terminal-before-space",
        "terminal-before-delimiter",
        "terminal-after-delimiter",
        "terminal-in-whitespace",
        "terminal-before-newline",
        "interior-whitespace",
        "adjacent-tables",
        "oversized-nonassessment",
        "escaped-pipe-whitespace-progress",
        "final-table-no-newline",
        "final-table-newline",
    ],
)
def test_long_review_parts_preserve_all_assessments_and_markdown_context(
    rationale_specs: tuple[int | str, ...] | None,
    preceding_paragraph_length: int,
    row_indentation: str,
    inline_details: bool,
    run_path: str,
    oversized_field: int | None,
    terminal_remainder: str | None,
    adjacent_tables: bool,
    oversized_nonassessment: bool,
    nonprogress: bool,
    final_table_newline: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrenderable_whitespace = any(
        isinstance(spec, str) and " " * _HELPERS["COMMENT_BYTE_LIMIT"] in spec
        for spec in rationale_specs or ()
    )
    if (
        oversized_field is not None
        or terminal_remainder is not None
        or unrenderable_whitespace
        or adjacent_tables
        or oversized_nonassessment
        or nonprogress
        or final_table_newline is not None
    ):
        review_path = tmp_path / "review.md"
        receipt = tmp_path / "receipt.txt"
        receipt.write_text("stale receipt\n")
        for name, value in {
            "HEAD_SHA": "a" * 40,
            "GITHUB_RUN_ID": "10",
            "GITHUB_RUN_ATTEMPT": "1",
            "RUN_URL": "https://example.test/run",
            "PR_NUMBER": "1",
            "REPO": "test/repo",
        }.items():
            monkeypatch.setenv(name, value)
        posts = []
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **kwargs: posts.append(Path(argv[-1]).read_text())
        )
    if nonprogress:
        split = _HELPERS["split_review"]
        encoded_lengths = []
        cuts = []

        # Bound the component fault before an empty slice can reach the parts list.
        class ProgressCheckedReview(str):
            def encode(self, encoding="utf-8", errors="strict"):
                if len(encoded_lengths) >= 2:
                    raise AssertionError("Splitter exceeded the two bounded iterations")
                encoded_lengths.append(len(self))
                return super().encode(encoding, errors)

            def __getitem__(self, key):
                if isinstance(key, slice):
                    if key.start is None and key.stop is not None:
                        if key.stop <= 0:
                            raise AssertionError("Splitter attempted a nonpositive cut")
                        cuts.append(key.stop)
                    return type(self)(super().__getitem__(key))
                return super().__getitem__(key)

        def bounded_split(_text: str, _limit: int) -> list[str]:
            return split(ProgressCheckedReview("|aa\\|  \n"), 4)

        monkeypatch.setitem(_HELPERS["review_comments"].__globals__, "split_review", bounded_split)
        review = _HELPERS["compose_review"](
            _REVIEW + "x" * _HELPERS["COMMENT_BYTE_LIMIT"],
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        review_path.write_text(review, encoding="utf-8")
        with pytest.raises(ValueError, match="split cannot make progress"):
            _HELPERS["publish_review"](review_path, receipt)
        assert encoded_lengths == [8, 5]
        assert cuts == [3]
        assert not posts
        assert not receipt.exists()
        assert not list(tmp_path.glob("comment*.md"))
        return

    if final_table_newline is not None:
        final_table = "\n| A | B |\n| --- | --- |\n| x | é🙂 |" + final_table_newline
        composed = _HELPERS["compose_review"](
            _REVIEW + "x" * 61000 + final_table,
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        assert composed.endswith(final_table)
        review_path.write_text(composed, encoding="utf-8")
        _HELPERS["publish_review"](review_path, receipt)
        paths = [
            tmp_path / "comment.md",
            *sorted(
                tmp_path.glob("comment-part-*.md"),
                key=lambda path: int(path.stem.rsplit("-", 1)[1]),
            ),
        ]
        comments = [path.read_text(encoding="utf-8") for path in paths]
        assert len(comments) > 1
        assert posts == [*comments[1:], comments[0]]
        assert receipt.read_text() == "a" * 40 + "\n"
        assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
        assert sum(comment.count(final_table) for comment in comments) == 1
        assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
        return

    if adjacent_tables:
        header, delimiter = _REVIEW.split("### Tests\n", 1)[1].splitlines()[:2]
        raw_limit = 59000
        prefix = _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
        assessment_table = "\n".join(_REVIEW.split("### Tests\n", 1)[1].splitlines()[:3]) + "\n"
        padding = raw_limit - len((prefix + "\n\n" + assessment_table).encode("utf-8"))
        prefix += "x" * padding + "\n\n" + assessment_table
        new_table = "\n| Label | Value |\n| --- | --- |\n| extra | retained |\n"
        adjacent_review = prefix + new_table + "Following paragraph.\n" * 100
        split = _HELPERS["split_review"]
        boundary_chunks = split(adjacent_review, raw_limit)
        assert len(prefix.encode("utf-8")) == raw_limit
        assert boundary_chunks[0] == prefix
        assert boundary_chunks[1].startswith(new_table)
        assert "".join(boundary_chunks) == adjacent_review
        # Pin a legal raw cut independently of the fully wrapped 60,000-byte cap.
        monkeypatch.setitem(
            _HELPERS["review_comments"].__globals__,
            "split_review",
            lambda text, limit: split(text, min(limit, raw_limit)),
        )
        review_path.write_text(adjacent_review, encoding="utf-8")
        _HELPERS["publish_review"](review_path, receipt)
        comments = [
            (tmp_path / "comment.md").read_text(),
            (tmp_path / "comment-part-2.md").read_text(),
        ]
        assert posts == [comments[1], comments[0]]
        assert receipt.read_text() == "a" * 40 + "\n"
        assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
        assert sum(comment.count(new_table) for comment in comments) == 1
        assert header not in comments[1]
        return

    rationales = (
        ["Guards the regression without a second setup."] * 1200
        if rationale_specs is None
        else ["x" * spec if isinstance(spec, int) else spec for spec in rationale_specs]
    )
    rows = "".join(
        f"{row_indentation}| tests/test_parser.py::test_case[{number}] | "
        f"Empty input é🙂 | Unit | Keep | {rationale} |\n"
        for number, rationale in enumerate(rationales)
    )
    if terminal_remainder is not None:
        rows = rows.replace(" |\n", " | \t\n", 1)
    if oversized_field is not None:
        field = ("tests/test_parser.py::test_case[0]", "Empty input é🙂", "Unit")[oversized_field]
        rows = rows.replace(field, field + "x" * 130000, 1)
    review = _REVIEW.replace(
        "| tests/test_parser.py::test_empty | Empty input | Unit | Keep |"
        " Guards the regression. |\n",
        rows,
    )
    if preceding_paragraph_length:
        review = review.replace(
            "### Tests\n", "### Tests\n" + "x" * preceding_paragraph_length + "\n\n"
        )
    if inline_details:
        review = review.replace("### Tests\n", "### Tests\n<details><summary>Logs</summary>\n\n")
        review = review.replace("### Scope\n", "</details>\n\n### Scope\n")
    if oversized_nonassessment:
        review = (
            "| Finding | Details |\n| --- | --- |\n| Large | " + "x" * 130000 + " |\n\n" + review
        )
    composed = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )
    if (
        oversized_field is not None
        or terminal_remainder is not None
        or unrenderable_whitespace
        or oversized_nonassessment
    ):
        review_path.write_text(composed, encoding="utf-8")
    if oversized_field is not None or unrenderable_whitespace or oversized_nonassessment:
        message = (
            "assessment fragments need all five populated fields"
            if unrenderable_whitespace
            else "comment capacity"
            if oversized_nonassessment
            else "table row cannot continue within comment capacity without five parsed fields"
        )
        if unrenderable_whitespace:
            assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        with pytest.raises(ValueError, match=message) as error:
            _HELPERS["publish_review"](review_path, receipt)
        assert not posts
        assert not receipt.exists()
        assert not list(tmp_path.glob("comment*.md"))
        if oversized_nonassessment:
            assert str(error.value) == (
                "Polly review table row cannot continue within comment capacity "
                "without five parsed fields."
            )
        return

    byte_limits = []
    if terminal_remainder is not None:
        split = _HELPERS["split_review"]

        def record_limit(text: str, limit: int) -> list[str]:
            byte_limits.append(limit)
            return split(text, limit)

        monkeypatch.setitem(_HELPERS["review_comments"].__globals__, "split_review", record_limit)

    if preceding_paragraph_length:
        header, delimiter = _REVIEW.split("### Tests\n", 1)[1].splitlines()[:2]
        header, delimiter = header + "\n", delimiter + "\n"
        header_start = composed.index(header)
        limit = len(composed[: header_start + len(header)].encode("utf-8"))
        boundary_chunks = _HELPERS["split_review"](composed, limit)
        assert boundary_chunks[0] == composed[:header_start]
        assert boundary_chunks[1].startswith(header + delimiter)
        assert "".join(boundary_chunks) == composed
    if any(isinstance(spec, str) and r"\|" in spec for spec in rationale_specs or ()):
        row = rows.splitlines()[0]
        boundary = row.index(r"\|") + 1
        limit = len(row[:boundary].encode("utf-8"))
        boundary_chunks = _HELPERS["split_review"](row, limit)
        assert boundary_chunks[0] == row[: boundary - 1]
        assert boundary_chunks[1].startswith(r"\|")
        assert "".join(boundary_chunks) == row
        assert all(len(chunk.encode("utf-8")) <= limit for chunk in boundary_chunks)
    chunks = _HELPERS["split_review"](composed, 58000)
    comments = _HELPERS["review_comments"](
        composed, head_sha="a" * 40, run_id="10-1", run_url=f"https://example.test/{run_path}"
    )
    if terminal_remainder is not None:
        _HELPERS["publish_review"](review_path, receipt)
        assert posts == [*comments[1:], comments[0]]
        assert receipt.read_text() == "a" * 40 + "\n"
        assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        # Pin the unadjusted byte cut independently of wrapper wording.
        row = rows.splitlines()[0]
        assert row.encode("utf-8")[3 * byte_limits[-1] :] == terminal_remainder.encode()

    header, delimiter = _REVIEW.split("### Tests\n", 1)[1].splitlines()[:2]
    for comment in comments:
        for line in comment.splitlines():
            if line.lstrip().startswith("|") and line.strip() not in {header, delimiter}:
                cells = _HELPERS["split_table_row"](line.strip().strip("|"))
                assert len(cells) == 5
                assert all(cell.strip() for cell in cells)

    assert "".join(chunks) == composed
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    assert all(comment.count("<details>") == comment.count("</details>") for comment in comments)
    assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
    assert "<!-- polly-reviewed-sha: " + "a" * 40 + " -->" in comments[0]
    for row, rationale in zip(rows.splitlines(), rationales, strict=True):
        if len(rationale.encode("utf-8")) > 60000:
            expected = _HELPERS["split_table_row"](row.strip().strip("|"))
            fragments = [
                _HELPERS["split_table_row"](line.strip().strip("|"))
                for comment in comments
                for line in comment.splitlines()
                if line.lstrip().startswith("|" + expected[0] + "|")
            ]
            assert len(fragments) >= 2
            assert all(len(cells) == 5 and cells[:4] == expected[:4] for cells in fragments)
            reconstructed = "".join(
                cells[4].removeprefix(" ").removesuffix(" ") for cells in fragments
            )
            assert reconstructed == rationale
        else:
            assert sum(comment.count(row) for comment in comments) == 1
    assert len(comments) >= 3
    for part, comment in enumerate(comments[1:], 2):
        assert f"part {part}/{len(comments)}" in comment
        assert (
            "| Test / case | Behavior protected | Layer | Needed? | Action / rationale |"
            in comment
        )
        assert "<!-- polly-reviewed-sha:" not in comment
    assert "## Blocking issues\nNone." in comments[0]
    assert "### Scope\nAll changes support the parser fix." in comments[-1]
    assert all("\n\n---\n<sub>Automated review by Polly" in comment for comment in comments)
    if terminal_remainder is not None:
        row = rows.splitlines()[0]
        context = _HELPERS["_markdown_context"](composed[: composed.index(row) + len(row)])
        assert "tests/test_parser.py::test_case[0]" not in context[0]
        assert not context[1].startswith(" |\n")
        assert context[2] == header + "\n" + delimiter + "\n"


def test_unicode_and_wrapper_bytes_are_included_in_comment_limit() -> None:
    review = "## Review\n```text\n" + "é🙂" * 24000 + "\n```\nFinal finding."
    chunks = _HELPERS["split_review"](review, 58000)
    review_metadata = {
        "head_sha": "b" * 40,
        "run_id": "10-1",
        "run_url": "https://example.test/" + "r" * 1000,
    }
    comments = _HELPERS["review_comments"](review, **review_metadata)

    assert "".join(chunks) == review
    assert all(len(chunk.encode("utf-8")) <= 58000 for chunk in chunks)
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    assert all(comment.count("```text") == comment.count("\n```\n") for comment in comments)
    assert sum(comment.count("é") for comment in comments) == 24000
    assert sum(comment.count("🙂") for comment in comments) == 24000
    assert "Final finding." in comments[-1]

    wrapper_bytes = len(_HELPERS["review_comments"]("", **review_metadata)[0].encode("utf-8"))
    boundary_review = "x" * (60000 - wrapper_bytes)
    exact_comments = _HELPERS["review_comments"](boundary_review, **review_metadata)
    assert len(exact_comments) == 1
    assert len(exact_comments[0].encode("utf-8")) == 60000

    overflow_review = boundary_review + "x"
    assert len(overflow_review.encode("utf-8")) + wrapper_bytes == 60001
    overflow_comments = _HELPERS["review_comments"](overflow_review, **review_metadata)
    assert len(overflow_comments) > 1
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in overflow_comments)


@pytest.mark.parametrize(
    "failed_call", [0, 1, 2, None], ids=["first-part", "later-part", "primary", "success"]
)
def test_multipart_publication_is_retryable_and_completes_only_after_primary(
    tmp_path: Path, failed_call: int | None
) -> None:
    review = tmp_path / "polly_review.txt"
    finding = "A preserved finding é🙂.\n"
    quoted_markers = (
        "```text\n\t<!-- polly-review-bot -->\n"
        "<!-- polly-reviewed-sha: " + "a" * 40 + " -->\n```\n"
    )
    review.write_text(_REVIEW + finding * 2300 + quoted_markers + finding * 2200, encoding="utf-8")
    receipt = tmp_path / "polly-completed-sha.txt"
    receipt.write_text("stale receipt\n")
    env = _fake_gh(
        tmp_path,
        "import subprocess\n"
        "directory = pathlib.Path(sys.argv[0]).parent\n"
        "arguments = sys.argv[1:]\n"
        "published = directory / 'published.json'\n"
        "bodies = json.loads(published.read_text()) if published.exists() else []\n"
        "if arguments[0] == 'api':\n"
        "    if arguments[1] == 'repos/test/repo/pulls/1':\n"
        "        print('a' * 40)\n"
        "    else:\n"
        "        assert arguments[1:3] == ['repos/test/repo/issues/1/comments', '--paginate']\n"
        "        comments = [dict(user=dict(type='Bot'), html_url=f'https://example.test/{i}', "
        "body=body) for i, body in enumerate(bodies)]\n"
        "        subprocess.run(['jq', '-r', arguments[arguments.index('--jq') + 1]], "
        "input=json.dumps(comments), text=True, check=True)\n"
        "    sys.exit(0)\n"
        "assert arguments[:5] == ['pr', 'comment', '1', '--repo', 'test/repo']\n"
        "assert arguments[5] == '--body-file'\n"
        "body = pathlib.Path(arguments[6]).read_text()\n"
        "if body.startswith('<!-- polly-skipped-sha:'):\n"
        "    (directory / 'skip.md').write_text(body)\n"
        "    sys.exit(0)\n"
        "assert not (directory / 'polly-completed-sha.txt').exists()\n"
        "record = directory / 'calls.json'\n"
        "calls = json.loads(record.read_text()) if record.exists() else []\n"
        "calls.append(body)\n"
        "record.write_text(json.dumps(calls))\n"
        f"if len(calls) - 1 == {failed_call!r}: sys.exit(42)\n"
        "published.write_text(json.dumps(bodies + [body]))\n",
    )
    env.update(
        HEAD_SHA="a" * 40,
        GITHUB_RUN_ID="10",
        GITHUB_RUN_ATTEMPT="1",
        RUN_URL="https://example.test/run",
        PR_NUMBER="1",
        REPO="test/repo",
    )
    workflow = yaml.safe_load((_ROOT / ".github/workflows/polly-review.yml").read_text())
    dedupe = next(step for step in workflow["jobs"]["review"]["steps"] if step.get("id") == "dupe")

    def already_reviewed() -> bool:
        output = tmp_path / "dedupe_output"
        output.write_text("")
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", dedupe["run"].replace("/tmp/", f"{tmp_path}/")],
            env={**env, "GITHUB_OUTPUT": str(output), "COMMENT_BODY": "", "FORCE_REVIEW": "false"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return "duplicate=true" in output.read_text()

    primary_header = "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "a" * 40 + " -->\n"
    command = [sys.executable, str(_SCRIPT), "publish", str(review), str(receipt)]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    calls = json.loads((tmp_path / "calls.json").read_text())
    if failed_call is not None:
        assert result.returncode == 42, result.stdout + result.stderr
        assert len(calls) == failed_call + 1
        assert not receipt.exists()
        published = tmp_path / "published.json"
        if published.exists():
            assert all(
                not body.startswith(primary_header) for body in json.loads(published.read_text())
            )
        assert not already_reviewed()
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        calls = json.loads((tmp_path / "calls.json").read_text())[failed_call + 1 :]
    assert result.returncode == 0, result.stdout + result.stderr
    assert receipt.read_text() == "a" * 40 + "\n"
    assert len(calls) == 3
    assert all(f"part {part}/3" in body for part, body in zip([2, 3, 1], calls, strict=True))
    assert all(not body.startswith(primary_header) for body in calls[:-1])
    assert calls[-1].startswith(primary_header)
    assert quoted_markers in calls[0]
    assert already_reviewed()


def test_pipes_inside_code_spans_do_not_split_test_rows() -> None:
    review = _REVIEW.replace(
        "| tests/test_parser.py::test_empty | Empty input | Unit | Keep |"
        " Guards the regression. |",
        "| `tests/test_parser.py::test_empty` | Successful `cd` survives `|| exit` before newline"
        " | Observer component | keep | Escaped \\| stays text. |",
    )

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )

    assert (
        "| `tests/test_parser.py::test_empty` | Successful `cd` survives "
        "`\\|\\| exit` before newline"
        " | Observer component | keep | Escaped \\| stays text. |"
    ) in result


def test_lone_backtick_falls_back_to_the_plain_split_and_keeps_delimiters() -> None:
    row = (
        "| `tests/test_parser.py::test_empty` | A lone ` backtick in input stays literal | Unit |"
        " keep | Asserts the `literal` token. |"
    )
    review = _REVIEW.replace(
        "| tests/test_parser.py::test_empty | Empty input | Unit | Keep |"
        " Guards the regression. |",
        row,
    )

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )

    assert row in result


def test_even_backslashes_before_pipe_keep_the_cell_intact() -> None:
    row = (
        r"| tests/test_parser.py::test_empty | Escaped \\| stays text | Unit | Keep | Guards it. |"
    )
    review = _REVIEW.replace(
        "| tests/test_parser.py::test_empty | Empty input | Unit | Keep |"
        " Guards the regression. |",
        row,
    )

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )

    assert row in result


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


def test_single_changed_file_accepts_case_rows_alongside_existing_tests_from_other_files() -> None:
    review = (
        _REVIEW.replace(
            "tests/test_parser.py::test_empty",
            "`test_empty[missing-input]` — added",
        )
        .replace(
            "### Tests\n",
            "### Tests\nChanged test file: `tests/test_parser.py`.\n",
        )
        .replace(
            "### Scope\n",
            "| `tests/test_other.py::test_existing` — unchanged | Existing behavior |"
            " Unit | keep | Still relevant. |\n"
            "### Scope\n",
        )
    )

    result = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )

    assert "`test_empty[missing-input]` — added" in result
    assert "`tests/test_other.py::test_existing` — unchanged" in result


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
    assert (tmp_path / "comment.md").read_text() == (
        "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: abc -->\n"
        "<!-- polly-review-run:10-1 -->\n"
        '## <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/'
        'docs/images/omnigent-logo.svg" alt="" height="20" valign="middle" /> Polly AI Review\n\n'
        + expected
        + "\n---\n<sub>Automated review by Polly · [workflow run](https://example.test/run)</sub>\n"
    )

    assert (tmp_path / "polly-completed-sha.txt").read_text().strip() == "abc"
    receipt = next(step for step in steps if step["name"] == "Upload Polly completion receipt")
    assert receipt["if"] == "steps.publish.outcome == 'success'"
    publish = next(step for step in steps if step["name"] == "Post review comment")
    assert publish["id"] == "publish"
    assert steps.index(publish) < steps.index(receipt)
    assert receipt["with"]["path"] == "/tmp/polly-completed-sha.txt"
    assert receipt["with"]["overwrite"] is True
    assert receipt["with"]["name"] == (
        "polly-completed-${{ steps.pr.outputs.pr_number }}-${{ steps.ctx.outputs.head_sha }}"
    )


def _fake_gh(directory: Path, script: str) -> dict[str, str]:
    gh = directory / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, pathlib, sys\n{script}")
    gh.chmod(0o755)
    return {**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


def _run_linked_issues(
    directory: Path, env: dict[str, str], pull_request: str = "test/repo#12"
) -> subprocess.CompletedProcess[str]:
    destination = directory / "linked_issues.json"
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "linked-issues", pull_request, str(destination)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def test_linked_issues_marks_truncated_bodies_and_omitted_issues(tmp_path: Path) -> None:
    limit = _HELPERS["LINKED_ISSUE_BODY_LIMIT"]
    nodes = [
        {
            "number": 7,
            "title": "Short",
            "state": "OPEN",
            "body": None,
            "repository": {"nameWithOwner": "test/repo"},
        },
        {
            "number": 8,
            "title": "Long",
            "state": "CLOSED",
            "body": "x" * (limit + 1),
            "repository": {"nameWithOwner": "other/repo"},
        },
        None,
    ]
    response = {
        "data": {
            "repository": {
                "pullRequest": {"closingIssuesReferences": {"totalCount": 4, "nodes": nodes}}
            }
        }
    }
    env = _fake_gh(
        tmp_path,
        "pathlib.Path(sys.argv[0]).with_name('args.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(json.dumps(response))})\n",
    )

    result = _run_linked_issues(tmp_path, env)

    assert result.stdout == ""
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:2] == ["api", "graphql"]
    assert {"owner=test", "name=repo", "number=12"} <= set(args)
    # -F sends the PR number as the Int the query declares; -f would send a string.
    assert args[args.index("number=12") - 1] == "-F"
    assert args[args.index("owner=test") - 1] == args[args.index("name=repo") - 1] == "-f"
    assert "closingIssuesReferences(first: 5)" in args[-1]
    assert json.loads((tmp_path / "linked_issues.json").read_text()) == {
        "issues": [
            {
                "issue": "test/repo#7",
                "title": "Short",
                "state": "OPEN",
                "body": "",
                "body_truncated": False,
            },
            {
                "issue": "other/repo#8",
                "title": "Long",
                "state": "CLOSED",
                "body": "x" * limit,
                "body_truncated": True,
            },
        ],
        "omitted": 2,
    }


_PARTIAL_NODE = {
    "data": {
        "repository": {
            "pullRequest": {
                "closingIssuesReferences": {
                    "totalCount": 1,
                    "nodes": [{"number": 7, "repository": {"nameWithOwner": "test/repo"}}],
                }
            }
        }
    }
}


@pytest.mark.parametrize(
    ("pull_request", "gh_script", "detail"),
    [
        ("test-repo-12", "sys.exit(1)\n", "ValueError"),
        (
            "test/repo#12",
            "sys.stderr.write('HTTP 401: Bad credentials\\n'); sys.exit(1)\n",
            "CalledProcessError: HTTP 401: Bad credentials",
        ),
        ("test/repo#12", "print('not json')\n", "JSONDecodeError"),
        ("test/repo#12", "print(json.dumps({'data': {'repository': None}}))\n", "TypeError"),
        (
            "test/repo#12",
            f"print({json.dumps(json.dumps(_PARTIAL_NODE))})\n",
            "KeyError: 'title'",
        ),
    ],
    ids=[
        "malformed-reference",
        "command-failure",
        "invalid-json",
        "missing-pull-request",
        "partial-node",
    ],
)
def test_linked_issues_lookup_failure_records_unavailable(
    tmp_path: Path, pull_request: str, gh_script: str, detail: str
) -> None:
    result = _run_linked_issues(tmp_path, _fake_gh(tmp_path, gh_script), pull_request)

    assert result.stdout.startswith("::warning::Could not read linked issues")
    assert detail in result.stdout
    assert result.stdout.count("\n") == 1
    assert json.loads((tmp_path / "linked_issues.json").read_text()) is None
