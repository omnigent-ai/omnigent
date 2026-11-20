"""Validate frozen-diff metrics and the summary publication gate without an LLM."""

import json
import os
import re
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


def _prepare_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_sha: str,
    run_url: str,
) -> tuple[Path, Path, list[str]]:
    review_path, receipt = tmp_path / "review.md", tmp_path / "receipt.txt"
    receipt.write_text("stale receipt\n")
    for name, value in {
        "HEAD_SHA": head_sha,
        "GITHUB_RUN_ID": "10",
        "GITHUB_RUN_ATTEMPT": "1",
        "RUN_URL": run_url,
        "PR_NUMBER": "1",
        "REPO": "test/repo",
    }.items():
        monkeypatch.setenv(name, value)
    posts: list[str] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: posts.append(Path(argv[-1]).read_text())
    )
    return review_path, receipt, posts


def _published_comments(review: str, review_path: Path, receipt: Path) -> list[str]:
    review_path.write_text(review, encoding="utf-8")
    _HELPERS["publish_review"](review_path, receipt)
    paths = [
        review_path.with_name("comment.md"),
        *sorted(
            review_path.parent.glob("comment-part-*.md"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        ),
    ]
    return [path.read_text(encoding="utf-8") for path in paths]


def _multipart_content(comment: str) -> str:
    _, marker, content = comment.partition("part 1 appears.\n\n")
    assert marker
    content, separator, footer = content.rpartition("\n\n---\n")
    assert separator and footer.startswith("<sub>Automated review by Polly")
    return content


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


def test_split_review_rejects_nonprogress_on_escaped_pipe_whitespace() -> None:
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

    with pytest.raises(ValueError, match="split cannot make progress"):
        split(ProgressCheckedReview("|aa\\|  \n"), 4)
    assert encoded_lengths == [8, 5]
    assert cuts == [3]


def _check_context_refusal(
    review: str,
    variant: str,
    short: bool,
    code_span_refusal: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reference_refusal: bool = False,
) -> None:
    review_path, receipt, posts = _prepare_publication(
        tmp_path, monkeypatch, head_sha="a" * 40, run_url="https://example.test/run"
    )
    if code_span_refusal or reference_refusal:
        message = (
            "Polly review reference definitions are not supported in multipart comments."
            if reference_refusal
            else r"Polly review noncanonical (details|summary)"
        )
        with pytest.raises(ValueError, match=re.escape(message) if reference_refusal else message):
            _published_comments(review, review_path, receipt)
        assert not posts and not receipt.exists()
        assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
        return
    with pytest.raises(ValueError) as caught:
        _published_comments(review, review_path, receipt)
    error = caught.value
    assert not variant.startswith("code-span-"), str(error)
    assert not short and variant not in {
        "container-bounded-blockquote",
        "fenced-container-literal",
    }
    assert variant not in {"boundary-table-fenced", "boundary-summary-fitting"}
    assert (
        not variant.startswith("boundary-inline-") or variant == "boundary-inline-oversized-guard"
    )
    if variant.startswith("boundary-"):
        assert str(error) in {
            "Polly review tables without leading pipes are not supported in multipart comments.",
            "Polly review literal container marker cannot fit within comment capacity.",
            "Polly review literal indentation cannot fit within comment capacity.",
            "Polly review disclosure header cannot fit within comment capacity.",
            "Polly review noncanonical summary is not supported in multipart comments.",
        }
    else:
        assert str(error) in {
            "Polly review Markdown container cannot fit within comment capacity.",
            "Polly review noncanonical details are not supported in multipart comments.",
        }
    assert not posts and not receipt.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]


def _resolved_destinations(tokens: list) -> list[tuple[str, str]]:
    return [
        (child.type, child.attrGet("href" if child.type == "link_open" else "src"))
        for token in tokens
        for child in token.children or []
        if child.type in {"link_open", "image"}
    ]


def _check_inline_code_context(
    table_variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from markdown_it import MarkdownIt

    short = table_variant.startswith("short-")
    variant = table_variant.removeprefix("short-")
    parser = MarkdownIt("commonmark").enable("table")
    metadata = {"head_sha": "a" * 40, "run_id": "10-1", "run_url": "https://example.test/run"}
    summary = {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    assessment = _REVIEW.replace("::test_empty", f"::test_empty[{table_variant}]")
    marker = "``" if variant.endswith("-double") else "`"
    literal = r"&amp; \* literal" + (" ` tick" if len(marker) == 2 else "")
    construct = marker + literal + marker
    if variant == "inline-code-oversized":
        expected_code = ["x" * 70000 + " " + literal]
        rationale = marker + expected_code[0] + marker
    elif short:
        expected_code = [literal]
        rationale = construct + "prose"
    else:
        calibration = _HELPERS["compose_review"](
            assessment.replace("Guards the regression.", "x" * 130000), summary
        )
        padding = [
            token.content
            for comment in _HELPERS["review_comments"](calibration, **metadata)
            for tokens in [parser.parse(comment)]
            for index, token in enumerate(tokens)
            if token.type == "inline" and tokens[index - 1].type == "td_open"
        ][4]
        assert padding and set(padding) == {"x"}
        if variant == "inline-code-repeated":
            unit = construct + "prose"
            rationale = "x" * ((len(padding) - len(marker) - 3) % len(unit)) + unit * 3000
            expected_code = [literal] * 3000
        else:
            rationale = "x" * (len(padding) - len(marker) - 3) + construct + "prose" + "y" * 70000
            expected_code = [literal]
    review = _HELPERS["compose_review"](
        assessment.replace("Guards the regression.", rationale), summary
    )
    original = parser.parse(review)
    cells = [
        token
        for index, token in enumerate(original)
        if token.type == "inline" and original[index - 1].type == "td_open"
    ]
    assert len(cells) == 5
    assert [
        child.content for child in cells[4].children if child.type == "code_inline"
    ] == expected_code
    assert (len(review.encode("utf-8")) < 60000) == short
    review_path, receipt, posts = _prepare_publication(
        tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
    )
    try:
        comments = _published_comments(review, review_path, receipt)
    except ValueError as error:
        assert variant == "inline-code-oversized" and not short
        assert str(error) == (
            "Polly review table row cannot continue within comment capacity "
            "without five parsed fields."
        )
        assert not posts and not receipt.exists()
        assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
        return
    assert posts == comments[1:] + comments[:1]
    assert receipt.read_text() == metadata["head_sha"] + "\n"
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
    rendered = [parser.parse(comment) for comment in comments]
    rendered_cells = []
    for tokens in rendered:
        footer = [
            token
            for token in tokens
            if token.type == "inline" and "Automated review by Polly" in token.content
        ]
        assert len(footer) == 1
        assert any(
            child.type == "html_inline" and child.content == "<sub>"
            for child in footer[0].children
        )
        data = [
            token
            for index, token in enumerate(tokens)
            if token.type == "inline" and tokens[index - 1].type == "td_open"
        ]
        if not data:
            continue
        assert len(data) == 5
        assert [token.content for token in data[:4]] == [token.content for token in cells[:4]]
        assert [
            token.content
            for index, token in enumerate(tokens)
            if token.type == "inline" and tokens[index - 1].type == "th_open"
        ] == [
            token.content
            for index, token in enumerate(original)
            if token.type == "inline" and original[index - 1].type == "th_open"
        ]
        rendered_cells.append(data[4])
    assert [
        child.content
        for cell in rendered_cells
        for child in cell.children
        if child.type == "code_inline"
    ] == expected_code, "A cut changed literal inline-code content or backtick pairing"
    assert "".join(child.content for cell in rendered_cells for child in cell.children) == "".join(
        child.content for child in cells[4].children
    ), "A cut changed parsed code/prose meaning"
    assert variant != "inline-code-oversized", (
        "An oversized atomic code span must refuse publication"
    )
    if short:
        assert len(comments) == 1
        assert comments[0].split("Polly AI Review\n\n", 1)[1].rsplit("\n---\n", 1)[0] == review
    else:
        assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
    return


def _check_review_context(
    table_variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html.parser import HTMLParser

    from markdown_it import MarkdownIt

    short = table_variant.startswith("short-")
    variant = table_variant.removeprefix("short-")
    table_rows = []
    assessment_review = _REVIEW
    comment_context = None
    reference_refusal = False
    code_span_refusal = variant in {
        "code-span-unmatched",
        "code-span-escaped-opener",
        "code-span-real-tag",
        "code-span-raw-html",
        "code-span-html-attribute",
        "code-span-link-title",
        "code-span-cross-line-ambiguity",
        "code-span-html-attribute-quoted-close",
        "code-span-link-title-quoted-close",
    }
    if variant.startswith("code-span-"):
        literal_context = re.fullmatch(
            r"code-span-(comparison|completed-link|completed-reference|less-equal|less-digit|"
            r"leading-autolink|leading-email)-"
            r"(details|summary)-(prose|rationale)",
            variant,
        )
        literals = {
            "code-span-details-prose": "`<details>` / `</details>`",
            "code-span-summary-prose": "`<summary>` / `</summary>`",
            "code-span-details-rationale": "`<details>` / `</details>`",
            "code-span-summary-rationale": "`<summary>` / `</summary>`",
            "code-span-matched-runs": "`` `<details>` and <summary> ``",
            "code-span-even-backslashes": r"\\`<details>`",
            "code-span-cut-guard": "`" + "x" * 130 + "<details> <summary>" + "y" * 130 + "`",
            "code-span-tags": "`<details>` and `<summary>`",
            "code-span-unmatched": "`<details>",
            "code-span-escaped-opener": r"\`<details>`",
            "code-span-real-tag": "`<summary>` and <details>",
            "code-span-raw-html": "",
            "code-span-html-attribute": '<span title="use `"> <details> literal? ` </span>',
            "code-span-link-title": '[link](https://x.test "use `") <details> literal? `',
            "code-span-cross-line-ambiguity": "`` earlier line\nNow `` <details> literal? ``",
            "code-span-comparison-cut-guard": "`"
            + "x" * 130
            + "<details> <summary>"
            + "y" * 130
            + "`",
            "code-span-completed-link-cut-guard": "`"
            + "x" * 130
            + "<details> <summary>"
            + "y" * 130
            + "`",
            "code-span-html-attribute-quoted-close": (
                '<span title="use > `"> <details> literal? ` </span>'
            ),
            "code-span-link-title-quoted-close": (
                '[link](https://x.test "use ) `") <details> literal? `'
            ),
            "code-span-comparison-tags": "Use x < 3 and `<details>` / `<summary>` tags.",
            "code-span-completed-link-tags": (
                "Use a [link](https://example.test/) and `<details>` / `<summary>` tags."
            ),
        }
        comment_context = re.fullmatch(
            r"code-span-comment-(standard|alternate|alternate-attribute)-"
            r"(details|summary)-(prose|rationale)",
            variant,
        )
        if comment_context:
            prefix = {
                "standard": "Use <!-- harmless --> and ",
                "alternate": "Use <!-- harmless --!> and ",
                "alternate-attribute": 'Use <!-- harmless --!> <span title="--> ',
            }[comment_context[1]]
            ending = '">logs</span>.' if comment_context[1] == "alternate-attribute" else " tags."
            literal = prefix + f"`<{comment_context[2]}>`" + ending
            code_span_refusal = not short and comment_context[1] != "standard"
        elif literal_context:
            prefix = {
                "comparison": "Use x < 3 and ",
                "completed-link": "Use a [link](https://example.test/) and ",
                "completed-reference": "Use a [link][docs] and ",
                "less-equal": "Use x <= 3 and ",
                "less-digit": "Use x <3 and ",
                "leading-autolink": "<https://example.test/> and ",
                "leading-email": "<reader@example.test> and ",
            }[literal_context[1]]
            literal = prefix + f"`<{literal_context[2]}>` tags."
        else:
            literal = literals[variant]
        lead = "Ordinary finding é🙂.\n" * (1 if short else 4500) + "\n"
        if literal_context and literal_context[1] == "completed-reference":
            lead = "[docs]: https://example.test/\n\n" + lead
        if variant.endswith("rationale"):
            assessment_review = _REVIEW.replace("Guards the regression.", literal).replace(
                "::test_empty", "::test_empty[param]"
            )
        elif variant == "code-span-raw-html":
            lead = (
                "<details><summary>Use `<summary>` tags</summary>\n\nBody\n\n</details>\n\n" + lead
            )
        elif variant in {
            "code-span-cut-guard",
            "code-span-comparison-cut-guard",
            "code-span-completed-link-cut-guard",
        }:
            prefix = {
                "code-span-cut-guard": " uses ",
                "code-span-comparison-cut-guard": " uses x < 3 and ",
                "code-span-completed-link-cut-guard": (
                    " uses a [link](https://example.test/) and "
                ),
            }[variant]
            lead = "x" * 59400 + prefix + literal + " tags.\n\n" + lead
        elif literal_context and literal_context[1].startswith("leading-"):
            lead = literal + "\n\n" + lead
        else:
            lead = "Wrap output in " + literal + " tags.\n\n" + lead
    elif variant.startswith("boundary-table-"):
        header, delimiter = "Finding | Reason", "--- | ---"
        if variant in {
            "boundary-table-delimiter-no-leading",
            "boundary-table-body-no-leading",
        }:
            header = "| " + header + " |"
        if variant in {"boundary-table-header-no-leading", "boundary-table-body-no-leading"}:
            delimiter = "| " + delimiter + " |"
        table_rows = [
            f"case{index} | rationale " + "x" * 150 for index in range(2 if short else 700)
        ]
        lead = header + "\n" + delimiter + "\n" + "\n".join(table_rows) + "\n\n"
        if variant == "boundary-table-fenced":
            lead = "```text\n" + lead + "```\n\n"
    elif variant.startswith("boundary-inline-"):
        marker = {
            "boundary-inline-quote": "> ",
            "boundary-inline-dash": "- ",
            "boundary-inline-plus": "+ ",
            "boundary-inline-star": "* ",
            "boundary-inline-dot": "1. ",
            "boundary-inline-paren": "2) ",
            "boundary-inline-whitespace": "       > ",
            "boundary-inline-overlap": "- \t> ",
            "boundary-inline-oversized-guard": " " * 70000 + "> ",
            "boundary-inline-indented": "    ",
            "boundary-inline-heading": "# ",
            "boundary-inline-thematic": "---\n\n",
        }[variant]
        left = "x" * (1 if "oversized" in variant else 59451)
        if variant == "boundary-inline-whitespace":
            left = left[:-7]
        lead = left + marker + "Finding remains ordinary prose\n\n" + "z" * 70000 + "\n\n"
    elif variant.startswith("boundary-summary"):
        opening, closing = "<details><summary>", "</summary>\n\nBody\n\n</details>\n\n"
        if variant == "boundary-summary-multiline":
            opening = "<details>\n<summary>\n"
            closing = "\n" + closing
        elif variant == "boundary-summary-attributes":
            opening = '<details><summary class="logs">'
        lead = opening + "x" * (20 if short or "fitting" in variant else 130000) + closing
        if variant == "boundary-summary-fitting":
            lead += "Ordinary finding outside the disclosure.\n" * 4000
    elif variant in {"container-blockquote", "container-bounded-blockquote"}:
        lead = "> Finding " + "x" * (10 if short or "bounded" in variant else 130000) + "\n\n"
    elif variant == "container-lazy-blockquote":
        lead = "> Finding start\n" + "continued paragraph text\n" * 4000 + "\n"
    elif variant in {
        "container-list-continuation",
        "container-ordered-continuation",
        "container-empty-list-continuation",
    }:
        ordered = variant == "container-ordered-continuation"
        lead = "7) Finding start\n\n" if ordered else "- Finding start\n"
        if variant == "container-empty-list-continuation":
            lead = "-\n"
        lead += ("   " if ordered else "  ") + "continued paragraph text\n"
        lead += (("   " if ordered else "  ") + "continued paragraph text\n") * (
            0 if short else 3999
        )
    elif variant == "fenced-container-literal":
        lead = (
            "```text\n"
            + "> Finding\n- item\n  continuation\n<details open>\n</details>\n" * 2000
            + "```\n\n"
        )
    else:
        opening = (
            '<details\n  class="logs">'
            if variant == "container-multiline-details"
            else "<details open>"
        )
        lead = (
            opening
            + "\n<summary>Logs</summary>\n\n"
            + "Log line.\n" * (2 if short else 8000)
            + "\n</details>\n\n"
        )
    if variant == "container-bounded-blockquote":
        lead += "Ordinary finding outside the quote.\n" * 4000
    review = lead + _HELPERS["compose_review"](
        assessment_review,
        {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
    )
    parser = MarkdownIt("commonmark").enable("table")

    def containers(tokens: list) -> list:
        stack, content = [], []
        for token in tokens:
            if token.type in {
                "blockquote_open",
                "bullet_list_open",
                "ordered_list_open",
                "list_item_open",
            }:
                stack.append((token.type, token.attrs))
            elif token.type in {
                "blockquote_close",
                "bullet_list_close",
                "ordered_list_close",
                "list_item_close",
            }:
                stack.pop()
            elif token.type == "inline" and stack:
                content.append((tuple(stack), token.content))
        return content

    class DisclosureContext(HTMLParser):
        depth = 0
        summary_depth = 0

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag == "details":
                self.depth += 1
            elif tag == "summary":
                assert self.summary_depth == 0
                self.summary_depth += 1
            elif tag == "sub":
                assert self.depth == 0, "Publication footer is inside a disclosure"
                assert self.summary_depth == 0, "Publication footer is inside a summary"

        def handle_endtag(self, tag: str) -> None:
            if tag == "summary":
                assert self.summary_depth == 1, (
                    "A rendered comment has an unmatched summary closer"
                )
                self.summary_depth -= 1
            elif tag == "details":
                assert self.summary_depth == 0, "A disclosure closes with its summary still open"
                assert self.depth > 0, "A rendered comment has an unmatched disclosure closer"
                self.depth -= 1

    class CommentEndContext(DisclosureContext):
        def __init__(self) -> None:
            super().__init__()
            self.code_elements = self.code_depth = 0

        def parse_comment(self, start: int, report: int = 1) -> int:
            # Model HTML's alternate comment end; CommonMark still uses its own parser.
            alternate = self.rawdata.find("--!>", start + 4)
            ordinary = self.rawdata.find("-->", start + 4)
            if alternate >= 0 and (ordinary < 0 or alternate < ordinary):
                if report:
                    self.handle_comment(self.rawdata[start + 4 : alternate])
                return alternate + 4
            return super().parse_comment(start, report)

        def handle_starttag(self, tag: str, attrs: list) -> None:
            super().handle_starttag(tag, attrs)
            if tag == "code":
                self.code_elements += 1
                self.code_depth += 1
            elif tag == "sub":
                assert self.code_depth == 0, "Publication footer is inside code"

        def handle_endtag(self, tag: str) -> None:
            super().handle_endtag(tag)
            if tag == "code":
                self.code_depth -= 1

    original = parser.parse(review)
    if comment_context:
        children = [child for token in parser.parse(literal) for child in token.children or []]
        assert [token.content for token in children if token.type == "code_inline"] == [
            f"<{comment_context[2]}>"
        ]
        reference = CommentEndContext()
        reference.feed(parser.render(literal))
        reference.close()
        assert reference.code_elements == (
            0 if comment_context[1] == "alternate-attribute" else 1
        ), "The HTML comment end changes whether CommonMark's code remains an element"
    if variant.startswith("code-span-") and literal_context:
        literal_tokens = [
            child
            for block in parser.parse(literal + "\n\n[docs]: https://example.test/\n")
            for child in block.children or []
        ]
        assert [token.content for token in literal_tokens if token.type == "code_inline"] == [
            f"<{literal_context[2]}>"
        ]
        assert not any(token.type == "html_inline" for token in literal_tokens)
        if literal_context[1].startswith(("completed-", "leading-")):
            assert any(token.type == "link_open" for token in literal_tokens)
        if literal_context[1] == "completed-reference":
            assert _resolved_destinations(original) == [("link_open", "https://example.test/")]
            reference_refusal = not short
    expected = containers(original)
    assert bool(expected) == ("blockquote" in variant or "continuation" in variant)
    optional_table = variant.startswith("boundary-table-") and variant != "boundary-table-fenced"
    if optional_table:
        assert sum(token.type == "table_open" for token in original) == 2
    else:
        assert sum(token.type == "table_open" for token in original) == 1
    assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
    assert (len(review.encode("utf-8")) < 60000) == short
    if code_span_refusal and comment_context is None:
        assert any(
            "<details>" in token.content
            for block in parser.parse(lead)
            for token in ([block] if block.type == "html_block" else block.children or [])
            if token.type in {"html_block", "html_inline"}
        ), "An ambiguous backtick must not hide an independently parsed real tag"
    unsupported = not short and (
        (variant.startswith("container-") and variant != "container-bounded-blockquote")
        or (variant.startswith("boundary-table-") and variant != "boundary-table-fenced")
        or (variant.startswith("boundary-summary") and variant != "boundary-summary-fitting")
        or variant == "boundary-inline-oversized-guard"
    )
    if code_span_refusal or unsupported or reference_refusal:
        _check_context_refusal(
            review,
            variant,
            short,
            code_span_refusal,
            tmp_path,
            monkeypatch,
            reference_refusal=reference_refusal,
        )
        return
    comments = _HELPERS["review_comments"](
        review, head_sha="a" * 40, run_id="10-1", run_url="https://example.test/run"
    )
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    if short:
        assert len(comments) == 1
        assert comments[0].split("Polly AI Review\n\n", 1)[1].rsplit("\n---\n", 1)[0] == review
    else:
        assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
    rendered = [parser.parse(comment) for comment in comments]
    assert [
        destination
        for tokens in rendered
        for destination in _resolved_destinations(tokens)
        if destination[1] != "https://example.test/run"
    ] == _resolved_destinations(original), "A comment lost an independently resolved destination"
    if comment_context:
        for comment in comments:
            reference = CommentEndContext()
            reference.feed(parser.render(comment))
            reference.close()
            assert reference.code_depth == reference.depth == reference.summary_depth == 0
    assert [item for tokens in rendered for item in containers(tokens)] == expected, (
        "Multipart publication changed independently parsed quote/list structure"
    )
    for tokens in rendered:
        disclosure = DisclosureContext()
        for token in tokens:
            if token.type == "html_block":
                disclosure.feed(token.content)
            for child in token.children or []:
                if child.type == "html_inline":
                    disclosure.feed(child.content)
            if token.type in {"fence", "code_block"}:
                assert "<sub>Automated review by Polly" not in token.content
                assert "tests/test_parser.py::test_empty" not in token.content
        assert disclosure.depth == 0, "A rendered comment leaves a disclosure open"
        assert disclosure.summary_depth == 0, "A rendered comment leaves a summary open"
    if optional_table:
        assert sum(token.type == "table_open" for tokens in rendered for token in tokens) >= 2
    else:
        assert sum(token.type == "table_open" for tokens in rendered for token in tokens) == 1
    if optional_table:
        for comment, tokens in zip(comments, rendered, strict=True):
            if re.search(r"case[0-9]+ \| rationale", comment):
                assert [
                    token.content
                    for index, token in enumerate(tokens)
                    if token.type == "inline" and tokens[index - 1].type == "th_open"
                ][:2] == ["Finding", "Reason"], "A continued table lost its parsed headers"
        assert [
            token.content
            for tokens in rendered
            for index, token in enumerate(tokens)
            if token.type == "inline"
            and tokens[index - 1].type == "td_open"
            and token.content.startswith("case")
        ] == [f"case{index}" for index in range(len(table_rows))]
    row = next(line for line in assessment_review.splitlines() if line.startswith("| tests/"))
    assert sum(comment.count(row) for comment in comments) == 1
    if variant.startswith("code-span-"):

        def literal_tags(tokens: list) -> list[str]:
            return [
                child.content
                for token in tokens
                for child in token.children or []
                if child.type == "code_inline"
                and re.search(r"</?(details|summary)", child.content)
            ]

        expected_literals = literal_tags(original)
        assert expected_literals
        assert [item for tokens in rendered for item in literal_tags(tokens)] == expected_literals
        if not short:
            fragments = [_multipart_content(comment) for comment in comments]
            assert "".join(fragments) == review
            if variant in {
                "code-span-cut-guard",
                "code-span-comparison-cut-guard",
                "code-span-completed-link-cut-guard",
            }:
                assert len(fragments[0]) <= review.index(literal)
                paragraph = review.split("\n\n", 1)[0]
                remaining = len(paragraph)
                pieces = []
                for fragment in fragments:
                    pieces.append(fragment[:remaining])
                    remaining -= len(pieces[-1])
                assert remaining == 0
                meanings = []
                for blocks in (
                    parser.parse(paragraph),
                    [block for piece in pieces for block in parser.parse(piece)],
                ):
                    meaning = []
                    for block in blocks:
                        for child in block.children or []:
                            item = (child.type, child.content)
                            if item[0] == "text" and meaning and meaning[-1][0] == "text":
                                meaning[-1] = ("text", meaning[-1][1] + item[1])
                            else:
                                meaning.append(item)
                    meanings.append(meaning)
                assert meanings[0] == meanings[1], "A cut lost parsed code/prose meaning"
    if variant == "container-bounded-blockquote":
        assert (
            sum(comment.count("Ordinary finding outside the quote.\n") for comment in comments)
            == 4000
        )
    if variant.startswith("boundary-inline-"):
        fragments = [_multipart_content(comment) for comment in comments]
        assert "".join(fragments) == review
        assert not any(
            token.type in {"code_block", "fence"} for tokens in rendered for token in tokens
        )
        fragment_tokens = [token for fragment in fragments for token in parser.parse(fragment)]
        for kind in (
            "blockquote_open",
            "bullet_list_open",
            "ordered_list_open",
            "heading_open",
            "hr",
            "code_block",
            "fence",
            "table_open",
        ):
            assert sum(token.type == kind for token in fragment_tokens) == sum(
                token.type == kind for token in original
            ), "A cut created a new block structure"
    if variant in {"fenced-container-literal", "boundary-table-fenced"}:
        remaining_code = "".join(token.content for token in original if token.type == "fence")
        for tokens in rendered:
            for token in tokens:
                if token.type == "fence":
                    fragment = token.content
                    if not remaining_code.startswith(fragment):
                        assert fragment.endswith("\n")
                        assert not fragment.endswith("\n\n")
                        fragment = fragment[:-1]
                    assert remaining_code.startswith(fragment)
                    remaining_code = remaining_code[len(fragment) :]
        assert not remaining_code
        remaining = review
        for number, comment in enumerate(comments):
            fragment = _multipart_content(comment)
            if number and not remaining.startswith(fragment):
                assert fragment.startswith("```text\n")
                fragment = fragment.removeprefix("```text\n")
            if not remaining.startswith(fragment):
                assert fragment.endswith("```\n")
                fragment = fragment.removesuffix("```\n")
                if not remaining.startswith(fragment):
                    assert fragment.endswith("\n")
                    assert not fragment.endswith("\n\n")
                    fragment = fragment[:-1]
            assert remaining.startswith(fragment)
            remaining = remaining[len(fragment) :]
        assert not remaining
    return


def _rendered_comments(review: str) -> list[str]:
    comments = _HELPERS["review_comments"](
        review, head_sha="a" * 40, run_id="10-1", run_url="https://example.test/run"
    )
    assert len(comments) > 1
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
    return comments


def _assert_fragments(
    fragments: list[list[str]],
    expected: list[str],
    rationale: str,
    *,
    minimum: int = 1,
    ordinary_empty_cell: bool = False,
) -> None:
    assert len(fragments) >= minimum
    assert all(len(cells) == 5 and cells[:4] == expected[:4] for cells in fragments)
    if ordinary_empty_cell:
        assert all(cells[1].strip() == "" for cells in fragments)
    else:
        assert all(all(cell.strip() for cell in cells) for cells in fragments)
    assert (
        "".join(cells[4].removeprefix(" ").removesuffix(" ") for cells in fragments) == rationale
    )


@pytest.mark.parametrize(
    ("form", "site", "short"),
    [
        (form, site, False)
        for form in (
            "entity",
            "decimal-entity",
            "hex-entity",
            "escaped-entity",
            "escape",
            "escape-underscore",
            "emphasis",
            "strong",
            "underscore",
            "nested-emphasis",
            "strike",
            "ambiguous-code",
        )
        for site in ("prose", "rationale")
    ]
    + [
        (form, site, True)
        for form in ("entity", "escape", "emphasis")
        for site in ("prose", "rationale")
    ]
    + [
        (form, "prose", short)
        for form in (
            "multiline-code",
            "multiline-double-code",
            "multiline-ambiguous-code",
            "multiline-emphasis",
            "hard-break",
        )
        for short in (False, True)
    ]
    + [
        ("oversized-multiline-code", "prose", False),
        ("oversized-multiline-ambiguous-code", "prose", False),
        ("intraword-underscore", "prose", False),
    ],
)
def test_multipart_inline_syntax_preserves_parsed_meaning(
    form: str,
    site: str,
    short: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from markdown_it import MarkdownIt

    construct = {
        "entity": "&amp;",
        "decimal-entity": "&#38;",
        "hex-entity": "&#x26;",
        "escaped-entity": r"\&amp;",
        "escape": r"\*",
        "escape-underscore": r"\_literal",
        "emphasis": "*highlight*",
        "strong": "**highlight**",
        "underscore": "_highlight_",
        "nested-emphasis": "***highlight***",
        "strike": "~~highlight~~",
        "ambiguous-code": "`unmatched ``literal &amp;``",
        "multiline-code": "`literal\ncode`",
        "multiline-double-code": "``literal ` tick\ncode``",
        "multiline-ambiguous-code": "`orphan ``literal\ncode``",
        "multiline-emphasis": "*first\nsecond*",
        "hard-break": "first\\\nsecond",
        "oversized-multiline-code": "`literal\ncode`",
        "oversized-multiline-ambiguous-code": "`orphan ``literal\ncode``",
        "intraword-underscore": "foo_bar_baz",
    }[form]
    oversized_paragraph = form in {
        "oversized-multiline-code",
        "oversized-multiline-ambiguous-code",
    }
    parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    metadata = {"head_sha": "a" * 40, "run_id": "10-1", "run_url": "https://example.test/run"}
    summary = {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    base = _HELPERS["compose_review"](_REVIEW, summary)
    if site == "rationale":
        calibration = _HELPERS["compose_review"](
            _REVIEW.replace("Guards the regression.", "x" * 130000), summary
        )
        capacity = len(
            [
                token.content
                for comment in _HELPERS["review_comments"](calibration, **metadata)
                for tokens in [parser.parse(_multipart_content(comment))]
                for index, token in enumerate(tokens)
                if token.type == "inline" and tokens[index - 1].type == "td_open"
            ][4]
        )
    else:
        calibration = "x" * 130000 + "\n\n" + base
        capacity = len(_multipart_content(_HELPERS["review_comments"](calibration, **metadata)[0]))
    if short:
        body = "before " + construct + " after"
    elif form == "intraword-underscore":
        calibration = "x" * 200000 + "\n\n" + base
        capacities = [
            len(_multipart_content(comment))
            for comment in _HELPERS["review_comments"](calibration, **metadata)
        ]
        body = (
            "x" * (capacities[0] - 3)
            + "foo_bar"
            + "x" * (capacities[1] - 5)
            + "_baz"
            + "y" * 70000
        )
        assert body[capacities[0]] == body[sum(capacities[:2]) - 1] == "_"
    elif "\n" in construct and not oversized_paragraph:
        paragraph = (
            "lead words " + construct
            if form == "multiline-ambiguous-code"
            else "lead words " * 3 + construct
        )
        padding = capacity - len(paragraph.split("\n", 1)[0]) - 3
        body = "x" * padding + "\n\n" + paragraph + "\n\n" + "y" * 70000
    else:
        inside = (
            construct.index("&amp;") + 2
            if form == "ambiguous-code"
            else 1
            if form in {"escape", "escape-underscore", "escaped-entity"}
            else 3
        )
        body = "x" * (capacity - inside - 1) + " " + construct + " " + "y" * 70000
    review = (
        _HELPERS["compose_review"](_REVIEW.replace("Guards the regression.", body), summary)
        if site == "rationale"
        else body + "\n\n" + base
    )

    def meaning(groups: list) -> list[tuple[str, str]]:
        result = []
        for group in groups:
            for child in group:
                item = (child.type, child.content)
                if item[0] == "text" and result and result[-1][0] == "text":
                    result[-1] = ("text", result[-1][1] + item[1])
                else:
                    result.append(item)
        return result

    original = parser.parse(review if site == "rationale" else body)
    original_cells = [
        token
        for index, token in enumerate(original)
        if token.type == "inline" and original[index - 1].type == "td_open"
    ]
    expected = meaning(
        [original_cells[4].children]
        if site == "rationale"
        else [token.children for token in original if token.type == "inline"]
    )
    if form in {"entity", "decimal-entity", "hex-entity"}:
        assert "&" in "".join(content for _, content in expected)
        assert construct not in "".join(content for _, content in expected)
    if form.startswith("multiline") or oversized_paragraph:
        assert any(kind in {"code_inline", "em_open"} for kind, _ in expected)
    if form in {"multiline-ambiguous-code", "oversized-multiline-ambiguous-code"}:
        assert [content for kind, content in expected if kind == "code_inline"] == ["literal code"]
    if form == "intraword-underscore":
        assert not any(kind in {"em_open", "strong_open"} for kind, _ in expected)
        assert sum(content.count("_") for _, content in expected) == 2
    if form == "escape-underscore":
        assert "_literal" in "".join(content for _, content in expected)
        assert construct not in "".join(content for _, content in expected)
    if oversized_paragraph:
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
        )
        try:
            _published_comments(review, review_path, receipt)
        except ValueError as error:
            assert oversized_paragraph and not short
            assert "paragraph" in str(error)
            assert not posts and not receipt.exists()
            assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
            return
        assert not oversized_paragraph, "An oversized dependent paragraph must refuse"
    comments = _HELPERS["review_comments"](review, **metadata)
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    groups = []
    for comment in comments:
        content = review if short else _multipart_content(comment)
        if site == "prose":
            content = content.partition("## Blocking issues\n")[0]
        tokens = parser.parse(content)
        if site == "rationale":
            cells = [
                token
                for index, token in enumerate(tokens)
                if token.type == "inline" and tokens[index - 1].type == "td_open"
            ]
            if cells:
                assert len(cells) == 5
                assert [token.content for token in cells[:4]] == [
                    token.content for token in original_cells[:4]
                ]
                groups.append(cells[4].children)
        else:
            groups.extend(token.children for token in tokens if token.type == "inline")
    assert meaning(groups) == expected, "Publication changed independently parsed inline meaning"
    if short:
        assert len(comments) == 1 and review in comments[0]
    else:
        assert len(comments) > 1


@pytest.mark.parametrize(
    ("form", "site", "boundary", "separated", "short"),
    [
        pytest.param(form, "rationale", boundary, False, False, id=f"{form}-{boundary}")
        for form in ("link", "image", "autolink")
        for boundary in ("label", "destination", "closing")
    ]
    + [
        pytest.param(form, "rationale", "destination", True, False, id=f"{form}-separated")
        for form in (
            "link",
            "image",
            "autolink",
            "email",
            "bare-url",
            "escaped",
            "inline-code",
            "inline-bare-code",
        )
    ]
    + [
        pytest.param(form, "prose", "destination", False, False, id=form)
        for form in (
            "multiline-label",
            "multiline-image",
            "multiline-title",
            "multiline-code",
            "multiline-relative-code",
            "multiline-relative-image",
            "multiline-fragment-code",
            "multiline-custom-code",
        )
    ]
    + [
        pytest.param("nested", "prose", "destination", True, False, id="nested-label-and-url"),
        pytest.param("fence", "prose", "destination", False, False, id="fenced-literal"),
        pytest.param("long-label", "prose", "destination", False, False, id="oversized-label"),
        pytest.param("long-url", "prose", "destination", False, False, id="oversized-url"),
    ]
    + [
        pytest.param(form, "prose", "destination", True, True, id=f"{form}-short")
        for form in (
            "link",
            "image",
            "autolink",
            "multiline-label",
            "multiline-relative-code",
            "multiline-fragment-code",
        )
    ],
)
def test_multipart_inline_destinations_keep_rendered_meaning(
    form: str,
    site: str,
    boundary: str,
    separated: bool,
    short: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from markdown_it import MarkdownIt

    destination = "https://example.test/proof"
    construct = {
        "link": f"[evidence]({destination})",
        "image": f"![evidence]({destination})",
        "autolink": f"<{destination}>",
        "email": "<evidence@example.test>",
        "bare-url": destination,
        "escaped": rf"\[evidence]({destination})",
        "inline-code": f"`[evidence]({destination})`",
        "inline-bare-code": f"`literal {destination} code`",
        "multiline-label": f"[evidence\nlabel]({destination})",
        "multiline-image": f"![evidence\nlabel]({destination})",
        "multiline-title": f'[evidence]({destination}\n "proof title")',
        "multiline-code": f"`literal evidence\n[label]({destination})\nend`",
        "multiline-relative-code": "`literal evidence\n[label](/docs)\nend`",
        "multiline-relative-image": "`literal evidence\n![evidence](../proof.png)\nend`",
        "multiline-fragment-code": "`literal evidence\n[label](#section)\nend`",
        "multiline-custom-code": "`literal evidence\n[label](guide:proof)\nend`",
        "nested": "[evidence [nested]](https://example.test/proof(a(b)c))",
        "fence": f"```text\n[evidence]({destination})\n```\n\n",
        "long-label": f"[{'e' * 70000}]({destination})",
        "long-url": f"[evidence]({destination + 'u' * 70000})",
    }[form]
    metadata = {"head_sha": "a" * 40, "run_id": "10-1", "run_url": "https://example.test/run"}
    summary = {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    base = _HELPERS["compose_review"](_REVIEW, summary)
    if site == "rationale":
        calibration = _HELPERS["compose_review"](
            _REVIEW.replace("Guards the regression.", "x" * 130000), summary
        )
        row = next(
            line
            for comment in _HELPERS["review_comments"](calibration, **metadata)
            for line in _multipart_content(comment).splitlines()
            if line.lstrip().startswith("|") and "tests/test_parser.py::test_empty" in line
        )
        padding = _HELPERS["split_table_row"](row.strip().strip("|"))[-1].strip()
        assert padding and set(padding) == {"x"}
        capacity = len(padding)
    else:
        calibration = "x" * 130000 + "\n\n" + base
        first = _multipart_content(_HELPERS["review_comments"](calibration, **metadata)[0])
        assert first and set(first) == {"x"}
        capacity = len(first)
    offset = (
        3
        if boundary == "label"
        else construct.index("example") + 3
        if "example" in construct and boundary == "destination"
        else len(construct) - 1
    )
    space = " " if separated else ""
    body = construct if short else "x" * (capacity - offset - len(space)) + space + construct
    if not short:
        body += space + "y" * 70000
    if form == "fence":
        body = "x" * capacity + "\n\n" + construct + "y" * 70000
    review = (
        _HELPERS["compose_review"](_REVIEW.replace("Guards the regression.", body), summary)
        if site == "rationale"
        else body + "\n\n" + base
    )
    parser = MarkdownIt("commonmark").enable("table")
    expected = _resolved_destinations(parser.parse(review))
    if form in {
        "bare-url",
        "escaped",
        "inline-code",
        "inline-bare-code",
        "multiline-code",
        "multiline-relative-code",
        "multiline-relative-image",
        "multiline-fragment-code",
        "multiline-custom-code",
        "fence",
    }:
        assert not expected
    else:
        assert len(expected) == 1
    assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
    assert (len(review.encode("utf-8")) < 60000) == short
    try:
        comments = _HELPERS["review_comments"](review, **metadata)
    except ValueError as error:
        assert not short and not separated
        assert "inline destination" in str(error) or str(error) == (
            "Polly review table row cannot continue within comment capacity "
            "without five parsed fields."
        )
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
        )
        with pytest.raises(ValueError) as publication_error:
            _published_comments(review, review_path, receipt)
        assert str(publication_error.value) == str(error)
        assert not posts and not receipt.exists()
        assert not list(tmp_path.glob("comment*.md"))
        return
    assert bool(len(comments) == 1) == short
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    actual = [
        item
        for comment in comments
        for item in _resolved_destinations(parser.parse(comment))
        if item[1] != metadata["run_url"]
    ]
    assert actual == expected, "Every destination must retain its independently rendered meaning"
    assert [
        child.content
        for comment in comments
        for token in parser.parse(review if short else _multipart_content(comment))
        for child in token.children or []
        if child.type == "code_inline"
    ] == [
        child.content
        for token in parser.parse(review)
        for child in token.children or []
        if child.type == "code_inline"
    ]
    if form == "bare-url":
        assert sum(construct in comment for comment in comments) == 1
    if short:
        assert review in comments[0]
    if form == "fence":
        assert [
            token.content
            for comment in comments
            for token in parser.parse(comment)
            if token.type == "fence"
        ] == [f"[evidence]({destination})\n"]


@pytest.mark.parametrize(
    "form",
    [
        "full",
        "collapsed",
        "shortcut",
        "image",
        "forward",
        "multiline-label",
        "escaped-label",
        "quoted-definition",
        "list-definition",
        "midline-definition",
        "code-span-completed-reference-details-prose",
        "code-span-completed-reference-details-rationale",
        "code-span-completed-reference-summary-prose",
        "code-span-completed-reference-summary-rationale",
        "short",
        "fenced-literal",
        "inline-control",
    ],
)
def test_multipart_reference_dependencies_are_refused(
    form: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if form.startswith("code-span-completed-reference-"):
        _check_review_context(form, tmp_path, monkeypatch)
        return

    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark").enable("table")
    destination = "https://example.test/reference"
    definition = f"[docs]: {destination}\n\n"
    use = "Use a [link][docs] here.\n"
    if form == "collapsed":
        use = "Use [docs][] here.\n"
    elif form == "shortcut":
        use = "Use [docs] here.\n"
    elif form == "image":
        use = "![description][docs]\n"
    elif form == "multiline-label":
        definition = f"[doc\nname]: {destination}\n\n"
        use = "Use a [link][doc name] here.\n"
    elif form == "escaped-label":
        definition = f"[doc\\]]: {destination}\n\n"
        use = "Use a [link][doc\\]] here.\n"
    elif form == "quoted-definition":
        definition = "> " + definition
    elif form == "list-definition":
        definition = "- " + definition
    elif form == "fenced-literal":
        definition = "```text\n" + definition + "```\n\n"
    elif form == "inline-control":
        definition = (
            "[A note] is ordinary prose; use a [link](https://example.test/direct) and "
            f"`[docs]: {destination}` literally.\n\n"
        )
    padding = "Ordinary finding.\n" * (1 if form == "short" else 4500) + "\n"
    lead = use + "\n" + padding + definition if form == "forward" else definition + padding + use
    if form == "midline-definition":
        calibration = (
            "x" * 130000
            + "\n\n"
            + _HELPERS["compose_review"](
                _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
            )
        )
        calibrated = _rendered_comments(calibration)[0]
        first_fragment = calibrated.split("part 1 appears.\n\n", 1)[1].rsplit("\n\n---\n", 1)[0]
        assert first_fragment == "x" * len(first_fragment)
        lead = first_fragment + definition + use + "\n" + padding
    review = (
        lead
        + "\n"
        + _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
    )
    original = parser.parse(review)
    expected = (
        []
        if form in {"fenced-literal", "midline-definition"}
        else [("link_open", "https://example.test/direct")]
        if form == "inline-control"
        else [("image" if form == "image" else "link_open", destination)]
    )
    assert _resolved_destinations(original) == expected
    assert sum(token.type == "table_open" for token in original) == 1
    assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
    assert (len(review.encode("utf-8")) < 60000) == (form == "short")
    if form == "midline-definition":
        assert _resolved_destinations(parser.parse(review[len(first_fragment) :])) == [
            ("link_open", destination)
        ], "The calibrated cut would manufacture a resolved reference without final preflight"
    if form not in {"short", "fenced-literal", "inline-control"}:
        _check_context_refusal(
            review,
            "reference-" + form,
            False,
            False,
            tmp_path,
            monkeypatch,
            reference_refusal=True,
        )
        return

    comments = _HELPERS["review_comments"](
        review, head_sha="a" * 40, run_id="10-1", run_url="https://example.test/run"
    )
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    if form == "short":
        assert len(comments) == 1
        fragments = [comments[0].split("Polly AI Review\n\n", 1)[1].rsplit("\n---\n", 1)[0]]
    else:
        assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
        fragments = [
            comment.split("part 1 appears.\n\n", 1)[1].rsplit("\n\n---\n", 1)[0]
            for comment in comments
        ]
    assert "".join(fragments) == review
    rendered = [parser.parse(comment) for comment in comments]
    assert [
        item
        for tokens in rendered
        for item in _resolved_destinations(tokens)
        if item[1] != "https://example.test/run"
    ] == expected, "Each comment must resolve its links independently"
    assert sum(token.type == "table_open" for tokens in rendered for token in tokens) == 1
    row = next(line for line in review.splitlines() if line.startswith("| tests/"))
    assert sum(comment.count(row) for comment in comments) == 1
    assert [token.content for tokens in rendered for token in tokens if token.type == "fence"] == [
        token.content for token in original if token.type == "fence"
    ]
    for tokens in rendered:
        assert not any(
            "<sub>Automated review by Polly" in token.content
            for token in tokens
            if token.type in {"fence", "code_block"}
        )
    assert [
        child.content
        for fragment in fragments
        for token in parser.parse(fragment)
        for child in token.children or []
        if child.type == "code_inline"
    ] == [
        child.content
        for token in original
        for child in token.children or []
        if child.type == "code_inline"
    ]


@pytest.mark.parametrize(
    "table_variant",
    [
        "container-blockquote",
        "container-list-continuation",
        "container-attributed-details",
        "container-lazy-blockquote",
        "container-ordered-continuation",
        "container-multiline-details",
        "short-container-blockquote",
        "short-container-list-continuation",
        "short-container-attributed-details",
        "container-bounded-blockquote",
        "fenced-container-literal",
        "container-empty-list-continuation",
        "boundary-table-no-leading",
        "boundary-table-header-no-leading",
        "boundary-table-delimiter-no-leading",
        "boundary-table-body-no-leading",
        "short-boundary-table-no-leading",
        "boundary-table-fenced",
        "boundary-inline-quote",
        "boundary-inline-dash",
        "boundary-inline-plus",
        "boundary-inline-star",
        "boundary-inline-dot",
        "boundary-inline-paren",
        "boundary-inline-whitespace",
        "boundary-inline-overlap",
        "boundary-inline-oversized-guard",
        "boundary-inline-indented",
        "boundary-inline-heading",
        "boundary-inline-thematic",
        "boundary-summary",
        "boundary-summary-multiline",
        "boundary-summary-attributes",
        "boundary-summary-fitting",
        "short-boundary-summary",
        "code-span-details-prose",
        "code-span-summary-prose",
        "code-span-details-rationale",
        "code-span-summary-rationale",
        "code-span-matched-runs",
        "code-span-even-backslashes",
        "code-span-cut-guard",
        "short-code-span-tags",
        "code-span-unmatched",
        "code-span-escaped-opener",
        "code-span-real-tag",
        "code-span-raw-html",
        "code-span-html-attribute",
        "code-span-link-title",
        "code-span-cross-line-ambiguity",
        "code-span-comparison-details-prose",
        "code-span-comparison-details-rationale",
        "code-span-comparison-summary-prose",
        "code-span-comparison-summary-rationale",
        "code-span-completed-link-details-prose",
        "code-span-completed-link-details-rationale",
        "code-span-completed-link-summary-prose",
        "code-span-completed-link-summary-rationale",
        "code-span-comparison-cut-guard",
        "code-span-completed-link-cut-guard",
        "code-span-html-attribute-quoted-close",
        "code-span-link-title-quoted-close",
        "short-code-span-comparison-tags",
        "short-code-span-completed-link-tags",
        "code-span-less-equal-details-prose",
        "code-span-less-equal-details-rationale",
        "code-span-less-equal-summary-prose",
        "code-span-less-equal-summary-rationale",
        "code-span-less-digit-details-prose",
        "code-span-less-digit-details-rationale",
        "code-span-less-digit-summary-prose",
        "code-span-less-digit-summary-rationale",
        "code-span-leading-autolink-details-prose",
        "code-span-leading-autolink-details-rationale",
        "code-span-leading-autolink-summary-prose",
        "code-span-leading-autolink-summary-rationale",
        "code-span-leading-email-details-prose",
        "code-span-leading-email-details-rationale",
        "code-span-leading-email-summary-prose",
        "code-span-leading-email-summary-rationale",
        "short-code-span-less-equal-details-prose",
        "short-code-span-less-equal-summary-prose",
        "short-code-span-less-digit-details-prose",
        "short-code-span-less-digit-summary-prose",
        "short-code-span-leading-autolink-details-prose",
        "short-code-span-leading-autolink-summary-prose",
        "short-code-span-leading-email-details-prose",
        "short-code-span-leading-email-summary-prose",
        "code-span-comment-standard-details-prose",
        "code-span-comment-standard-details-rationale",
        "code-span-comment-standard-summary-prose",
        "code-span-comment-standard-summary-rationale",
        "code-span-comment-alternate-details-prose",
        "code-span-comment-alternate-details-rationale",
        "code-span-comment-alternate-summary-prose",
        "code-span-comment-alternate-summary-rationale",
        "code-span-comment-alternate-attribute-details-prose",
        "code-span-comment-alternate-attribute-details-rationale",
        "code-span-comment-alternate-attribute-summary-prose",
        "code-span-comment-alternate-attribute-summary-rationale",
        "short-code-span-comment-alternate-details-prose",
        "short-code-span-comment-alternate-details-rationale",
        "short-code-span-comment-alternate-summary-prose",
        "short-code-span-comment-alternate-summary-rationale",
        "short-code-span-completed-reference-details-prose",
        "short-code-span-completed-reference-details-rationale",
        "short-code-span-completed-reference-summary-prose",
        "short-code-span-completed-reference-summary-rationale",
        "inline-code-oversized",
        "inline-code-repeated",
        "inline-code-calibrated",
        "inline-code-calibrated-double",
        "short-inline-code-calibrated",
        "short-inline-code-calibrated-double",
    ],
)
def test_review_comments_preserve_supported_markdown_context(
    table_variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if table_variant.removeprefix("short-").startswith("inline-code-"):
        _check_inline_code_context(table_variant, tmp_path, monkeypatch)
    else:
        _check_review_context(table_variant, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "table_variant",
    [
        "oversized-header",
        "part-limit",
        "work-limit",
        "fenced-header-control",
        "oversized-fence-backticks",
        "oversized-fence-tildes",
        "unsafe-inline-run-backticks",
        "unsafe-inline-run-tildes",
        "unsafe-fence-whitespace-backticks",
        "unsafe-fence-whitespace-tildes",
    ],
)
def test_review_comments_enforce_capacity_and_work_limits(
    table_variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _REVIEW
    if table_variant.startswith(
        ("oversized-fence-", "unsafe-inline-run-", "unsafe-fence-whitespace-")
    ):
        char = "`" if table_variant.endswith("backticks") else "~"
        fence = char * 3
        if table_variant.startswith("oversized-fence-"):
            fence = char * 70000
            code = "literal" + "\n"
            message = "fence delimiter cannot fit within comment capacity"
        else:
            code = (
                "x" + char * 130000 + "a" + "\n"
                if table_variant.startswith("unsafe-inline-run-")
                else fence + " " * 130000 + "a" + "\n"
            )
            message = "literal fence run cannot fit within comment capacity"
        review = fence + "text\n" + code + fence + "\n\n" + review
    elif table_variant in {"oversized-header", "fenced-header-control"}:
        table = "| " + "H" * 70000 + " | Details |\n| --- | --- |\n| item | note |\n"
        review = (
            ("```text\n" + table + "```\n" if table_variant == "fenced-header-control" else table)
            + "\n"
            + review
        )
        message = "table header cannot fit within comment capacity"
    else:
        review = review.replace("Empty input", "Empty input" + "b" * 31000)
        review = review.replace("Guards the regression.", "x" * 70000)
        setting, limit, message = (
            ("MAX_REVIEW_PARTS", 2, "part limit")
            if table_variant == "part-limit"
            else ("RENDER_WORK_BYTE_LIMIT", 1, "rendering work budget")
        )
        monkeypatch.setitem(_HELPERS["review_comments"].__globals__, setting, limit)
    composed = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )
    if table_variant == "fenced-header-control":
        comments = _rendered_comments(composed)
        assert sum(comment.count("H") for comment in comments) == 70000
        assert sum(comment.count("| item | note |") for comment in comments) == 1
        assert all(comment.count("```text") == comment.count("\n```\n") for comment in comments)
    else:
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha="a" * 40, run_url="https://example.test/run"
        )
        review_path.write_text(composed, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            _HELPERS["publish_review"](review_path, receipt)
        assert not posts
        assert not receipt.exists()
        assert not list(tmp_path.glob("comment*.md"))
        if table_variant == "part-limit":
            raw_ceiling = _HELPERS["COMMENT_BYTE_LIMIT"] * limit
            assert len(composed.encode("utf-8")) < raw_ceiling
            initial_oversized = "é" * (raw_ceiling // 2 + 1) + "\n\n" + composed
            assert len(initial_oversized.encode("utf-8")) > raw_ceiling

            def unexpected_context(*args, **kwargs):
                pytest.fail("Initial raw-byte refusal must precede Markdown context rendering")

            with monkeypatch.context() as context:
                context.setitem(
                    _HELPERS["review_comments"].__globals__,
                    "_markdown_context",
                    unexpected_context,
                )
                with pytest.raises(
                    ValueError, match=r"^Polly review exceeds the publication part limit\.$"
                ):
                    _published_comments(initial_oversized, review_path, receipt)
            assert not posts and not receipt.exists()
            assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
    return


@pytest.mark.parametrize(
    "table_variant",
    [
        "ordinary-empty-cell",
        "incremental-work",
    ],
)
def test_review_comments_preserve_identity_with_bounded_work(
    table_variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    large_identity = table_variant == "incremental-work"
    rationale = "x" * 70000
    header, delimiter, original = _REVIEW.split("### Tests\n", 1)[1].split("\n")[:3]
    if large_identity:
        row = original.replace("Empty input", "Empty input" + "b" * 31000)
        row = row.replace("Guards the regression.", rationale)
        review = _REVIEW.replace(original, row)
    else:
        header = "| Item | Details | Kind | Action | Notes |"
        row = "| finding-0 | | Info | Keep | " + rationale + " |"
        review = header + "\n" + delimiter + "\n" + row + "\n\n" + _REVIEW
    composed = _HELPERS["compose_review"](
        review, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )
    if large_identity:
        metadata = {
            "head_sha": "a" * 40,
            "run_id": "10-1",
            "run_url": "https://example.test/run",
        }
        smaller = composed.replace(rationale, "x" * 20000)
        small_comments = _HELPERS["review_comments"](smaller, **metadata)
        assert len(small_comments) == 1
        assert len(small_comments[0].encode("utf-8")) < 52000
        with monkeypatch.context() as render_patch:
            context = _HELPERS["_markdown_context"]
            visited = set()

            def checked_context(text: str, **kwargs):
                if text != composed and not text.startswith("<!--"):
                    assert text not in visited, "Accepted review prefix was parsed again"
                    assert len(visited) < 32, "Exceeded bounded component context visits"
                    visited.add(text)
                return context(text, **kwargs)

            render_patch.setitem(
                _HELPERS["review_comments"].__globals__,
                "_markdown_context",
                checked_context,
            )
            comments = _HELPERS["review_comments"](composed, **metadata)
    else:
        comments = _rendered_comments(composed)
    expected = _HELPERS["split_table_row"](row.strip().strip("|"))
    fragments = []
    for comment in comments:
        rows = [line for line in comment.split("\n") if line.startswith("|" + expected[0] + "|")]
        if rows:
            assert header + "\n" + delimiter in comment
        for line in rows:
            cells = _HELPERS["split_table_row"](line.strip().strip("|"))
            fragments.append(cells)
    assert len(comments) > 1
    _assert_fragments(
        fragments, expected, rationale, minimum=2, ordinary_empty_cell=not large_identity
    )
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    if large_identity:
        later_rationale = "Retains the later independent identity."
        later_row = original.replace("test_empty", "test_later").replace(
            "Empty input", "Different input" + "c" * 40000
        )
        later_row = later_row.replace("Guards the regression.", later_rationale)
        later_review = _HELPERS["compose_review"](
            _REVIEW.replace(original, later_row),
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        later_comments = _HELPERS["review_comments"](later_review, **metadata)
        assert len(later_comments) == 1
        assert len(later_comments[0].encode("utf-8")) < 60000
        comments = _rendered_comments(composed.replace(row, row + "\n" + later_row))
        assert sum(comment.count(later_row) for comment in comments) == 1
        for expected_row, expected_rationale in [
            (row, rationale),
            (later_row, later_rationale),
        ]:
            expected = _HELPERS["split_table_row"](expected_row.strip().strip("|"))
            fragments = []
            for comment in comments:
                for line in comment.split("\n"):
                    if line.startswith("|" + expected[0] + "|"):
                        assert header + "\n" + delimiter in comment
                        fragments.append(_HELPERS["split_table_row"](line.strip().strip("|")))
            _assert_fragments(fragments, expected, expected_rationale)
    return


def test_multipart_nested_lists_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from markdown_it import MarkdownIt

    findings = "## Findings\n\n" + "".join(
        f"- Finding {index}\n    - nested {index} " + "z" * 200 + "\n" for index in range(400)
    )
    review = (
        findings
        + "\n"
        + _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
    )
    parser = MarkdownIt("commonmark").enable("table")
    original = parser.parse(review)
    expected = [
        item
        for index in range(400)
        for item in [(f"Finding {index}", 3), (f"nested {index} " + "z" * 200, 5)]
    ]
    assert [
        (token.content, token.level)
        for token in original
        if token.type == "inline" and token.content.startswith(("Finding ", "nested "))
    ] == expected
    assert not any(token.type in {"code_block", "fence"} for token in original)
    assert sum(token.type == "table_open" for token in original) == 1
    assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
    assert len(review.encode("utf-8")) > 60000
    review_path, receipt, posts = _prepare_publication(
        tmp_path, monkeypatch, head_sha="a" * 40, run_url="https://example.test/run"
    )
    with pytest.raises(ValueError) as error:
        _published_comments(review, review_path, receipt)
    assert str(error.value) == "Polly review nested lists are not supported in multipart comments."
    assert not posts and not receipt.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
    return


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
        "final_table_newline",
        "rationale_separator",
    ),
    [
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="many-rows",
        ),
        pytest.param(
            (20000, 45000, 20000),
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="unequal-rows",
        ),
        pytest.param(
            (3000,) * 30,
            59194,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="header-boundary",
        ),
        pytest.param(
            None,
            0,
            "  ",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="indented-rows",
        ),
        pytest.param(
            (70000,),
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="oversized-row",
        ),
        pytest.param(
            ("y" * 69 + ("x" * 70 + "\\|") * 1100,),
            0,
            "",
            False,
            "r" * 66,
            None,
            None,
            False,
            False,
            None,
            None,
            id="escaped-pipe-boundary",
        ),
        pytest.param(
            None,
            0,
            "",
            True,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="inline-details",
        ),
        pytest.param(
            ("Reason",),
            0,
            "",
            False,
            "run",
            0,
            None,
            False,
            False,
            None,
            None,
            id="oversized-test",
        ),
        pytest.param(
            ("Reason",),
            0,
            "",
            False,
            "run",
            1,
            None,
            False,
            False,
            None,
            None,
            id="oversized-behavior",
        ),
        pytest.param(
            ("Reason",),
            0,
            "",
            False,
            "run",
            2,
            None,
            False,
            False,
            None,
            None,
            id="oversized-layer",
        ),
        pytest.param(
            (177655, "Tail."),
            0,
            "",
            False,
            "run",
            None,
            " | \t",
            False,
            False,
            None,
            None,
            id="terminal-before-space",
        ),
        pytest.param(
            (177654, "Tail."),
            0,
            "",
            False,
            "run",
            None,
            "| \t",
            False,
            False,
            None,
            None,
            id="terminal-before-delimiter",
        ),
        pytest.param(
            (177653, "Tail."),
            0,
            "",
            False,
            "run",
            None,
            " \t",
            False,
            False,
            None,
            None,
            id="terminal-after-delimiter",
        ),
        pytest.param(
            (177652, "Tail."),
            0,
            "",
            False,
            "run",
            None,
            "\t",
            False,
            False,
            None,
            None,
            id="terminal-in-whitespace",
        ),
        pytest.param(
            (177651, "Tail."),
            0,
            "",
            False,
            "run",
            None,
            "",
            False,
            False,
            None,
            None,
            id="terminal-before-newline",
        ),
        pytest.param(
            ("Reason." + " " * 135000 + "Tail.",),
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            None,
            id="interior-whitespace",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            True,
            False,
            None,
            None,
            id="adjacent-tables",
        ),
        pytest.param(
            ("Reason",),
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            True,
            None,
            None,
            id="oversized-nonassessment",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            "",
            None,
            id="final-table-no-newline",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            "\n",
            None,
            id="final-table-newline",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            "\u2028",
            id="rationale-line-separator",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            "\x85",
            id="rationale-next-line",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            "\x0c",
            id="rationale-form-feed",
        ),
        pytest.param(
            None,
            0,
            "",
            False,
            "run",
            None,
            None,
            False,
            False,
            None,
            " ",
            id="rationale-space-control",
        ),
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
    final_table_newline: str | None,
    rationale_separator: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrenderable_whitespace = any(
        isinstance(spec, str) and " " * _HELPERS["COMMENT_BYTE_LIMIT"] in spec
        for spec in rationale_specs or ()
    )
    if oversized_field is not None or unrenderable_whitespace or oversized_nonassessment:
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha="a" * 40, run_url="https://example.test/run"
        )

    if final_table_newline is not None:
        final_table = "\n| A | B |\n| --- | --- |\n| x | é🙂 |" + final_table_newline
        composed = _HELPERS["compose_review"](
            _REVIEW + "x" * 61000 + final_table,
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        assert composed.endswith(final_table)
        comments = _rendered_comments(composed)
        assert len(comments) > 1
        assert sum(comment.count(final_table) for comment in comments) == 1
        return

    if rationale_separator is not None:
        rationale = "Start." + rationale_separator + "y" * 70000 + " tail"
        composed = _HELPERS["compose_review"](
            _REVIEW.replace("Guards the regression.", rationale),
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        comments = _rendered_comments(composed)
        header, delimiter, row = _REVIEW.split("### Tests\n", 1)[1].split("\n")[:3]
        expected = _HELPERS["split_table_row"](row.strip().strip("|"))
        fragments = []
        for part, comment in enumerate(comments):
            rows = [
                line
                for line in comment.split("\n")
                if line.lstrip().startswith("|") and line.strip() not in {header, delimiter}
            ]
            if part:
                assert header + "\n" + delimiter in comment
                assert rows
            for line in rows:
                cells = _HELPERS["split_table_row"](line.strip().strip("|"))
                fragments.append(cells)
        _assert_fragments(fragments, expected, rationale, minimum=2)
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
            lambda text, limit, **kwargs: split(text, min(limit, raw_limit), **kwargs),
        )
        comments = _rendered_comments(adjacent_review)
        assert len(comments) == 2
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
    if oversized_field is not None or unrenderable_whitespace or oversized_nonassessment:
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
        assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        # Pin the unadjusted byte cut independently of wrapper wording.
        row = rows.splitlines()[0]
        raw_limit = 59243
        assert row.encode("utf-8")[3 * raw_limit :] == terminal_remainder.encode()
        boundary_chunks = _HELPERS["split_review"](row, raw_limit)
        assert "".join(boundary_chunks) == row
        assert all(len(chunk.encode("utf-8")) <= raw_limit for chunk in boundary_chunks)
        assert all(chunk.strip(" |\t") for chunk in boundary_chunks)

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
            _assert_fragments(fragments, expected, rationale, minimum=2)
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


@pytest.mark.parametrize(
    ("kind", "form"),
    [
        ("html-comment", "oversized"),
        ("html-pre", "oversized"),
        ("html-comment", "boundary"),
        ("html-pre", "boundary"),
        ("html-comment", "unclosed"),
        ("html-pre", "unclosed"),
        ("html-cdata", "boundary"),
        ("html-instruction", "boundary"),
        ("html-textarea", "boundary"),
        ("html-generic", "unsupported"),
        ("html-pre-attribute", "unsupported"),
        ("html-pre-open-attribute", "unsupported"),
        ("html-comment", "literal-markers"),
        ("html-pre", "literal-markers"),
        ("html-comment", "table-boundary"),
        ("html-pre", "table-boundary"),
        ("inline-comment", "oversized"),
        ("paragraph-indent", "boundary"),
        ("paragraph-tab", "boundary"),
        ("paragraph-indent", "oversized"),
        ("indented-code", "oversized"),
        ("indented-code", "fitting"),
        ("indented-code", "blank-lines"),
        ("setext-dash", "boundary"),
        ("setext-equal", "boundary"),
        ("setext-dash", "multiline"),
        ("setext-equal", "oversized"),
        ("html-comment", "short"),
        ("html-pre", "short"),
        ("html-generic", "short"),
        ("paragraph-indent", "short"),
        ("indented-code", "short"),
        ("setext-dash", "short"),
        ("html-pre", "closing-tail-context"),
        ("html-script", "closing-tail-context"),
        ("html-style", "closing-tail-context"),
        ("html-textarea", "closing-tail-context"),
        ("html-comment", "closing-tail-context"),
        ("html-cdata", "closing-tail-context"),
        ("html-instruction", "closing-tail-context"),
        ("html-declaration", "closing-tail-context"),
        ("html-pre", "closing-tail-cdata"),
        ("html-comment", "closing-tail-instruction"),
        ("html-pre", "closing-tail-multiline"),
        ("html-comment", "closing-tail-multiline"),
        ("html-pre", "closing-tail-plain"),
        ("html-comment", "closing-tail-plain"),
        ("html-pre", "closing-tail-whitespace"),
        ("html-script", "closing-tail-whitespace"),
        ("html-style", "closing-tail-whitespace"),
        ("html-textarea", "closing-tail-whitespace"),
        ("html-comment", "closing-tail-whitespace"),
        ("html-cdata", "closing-tail-whitespace"),
        ("html-instruction", "closing-tail-whitespace"),
        ("html-declaration", "closing-tail-whitespace"),
        ("html-pre", "closing-tail-short"),
        ("html-comment", "closing-tail-short"),
        ("indented-disclosure", "outside-open"),
        ("tab-disclosure", "outside-open"),
        ("indented-disclosure", "inside-close"),
        ("tab-disclosure", "inside-close"),
        ("indented-disclosure", "outside-summary"),
        ("tab-disclosure", "inside-summary"),
        ("indented-disclosure", "short-outside-open"),
        ("tab-disclosure", "short-inside-close"),
        ("indented-disclosure", "fenced-literal"),
        ("tab-disclosure", "raw-literal"),
        ("commented-disclosure", "open-comment-close"),
        ("commented-disclosure", "close-comment-open"),
        ("commented-disclosure", "summary-comment-close"),
        ("commented-disclosure", "summary-comment-open"),
        ("commented-disclosure", "summary-comment-tags"),
        ("commented-disclosure", "benign-comment-boundaries"),
        ("commented-disclosure", "short-open-comment-close"),
        ("commented-disclosure", "short-summary-comment-close"),
        ("commented-disclosure", "fenced-literal"),
        ("commented-disclosure", "raw-literal"),
    ],
    ids=lambda value: value,
)
def test_multipart_keeps_literal_blocks_and_paragraph_dependencies_indivisible(
    kind: str, form: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html.parser import HTMLParser

    from markdown_it import MarkdownIt

    metadata = {"head_sha": "a" * 40, "run_id": "10-1", "run_url": "https://example.test/run"}
    composed = _HELPERS["compose_review"](
        _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
    )
    parser = MarkdownIt("commonmark").enable("table")

    class FooterContext(HTMLParser):
        footers = 0
        raw_depth = 0
        details_depth = 0
        details_openings = 0
        comment_tag_lookalikes = 0

        def handle_comment(self, data: str) -> None:
            self.comment_tag_lookalikes += bool(re.search(r"</?(details|summary)\b", data))

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag in {"pre", "textarea", "code"}:
                self.raw_depth += 1
            elif tag == "details":
                self.details_depth += 1
                self.details_openings += 1
            elif tag == "sub":
                assert self.raw_depth == 0, "Footer is inside literal HTML or code"
                assert self.details_depth == 0, "Footer is inside a disclosure"
                self.footers += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in {"pre", "textarea", "code"}:
                self.raw_depth -= 1
            elif tag == "details":
                assert self.details_depth > 0, "A comment has an unmatched disclosure closer"
                self.details_depth -= 1

    calibration = _HELPERS["review_comments"]("x" * 130000 + "\n\n" + composed, **metadata)
    capacity = len(_multipart_content(calibration[0]))
    assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
    amount = 130000 if form == "oversized" else 100
    prefix = ""
    indented_disclosure = kind in {"indented-disclosure", "tab-disclosure"}
    commented_disclosure = kind == "commented-disclosure"
    literal_control = form in {"fenced-literal", "raw-literal"}
    if indented_disclosure:
        indentation = "\t" if kind == "tab-disclosure" else "    "
        literal = (
            "</details>"
            if "close" in form
            else "<summary>Literal summary</summary>"
            if "summary" in form
            else "<details>"
        )
        block = indentation + literal + "\n\n"
        if form == "fenced-literal":
            block = "```text\n" + block + "```\n\n"
        elif form == "raw-literal":
            block = "<!--\n" + block + "-->\n\n"
        if "inside" in form:
            prefix = "<details>\n<summary>Real disclosure</summary>\n\n"
    elif commented_disclosure:
        variant = form.removeprefix("short-")
        if variant == "open-comment-close":
            block = "<details><!-- </details> -->\n<summary>Real disclosure</summary>\n\n"
        elif variant == "close-comment-open":
            block = (
                "<details>\n<summary>Real disclosure</summary>\n\nBody.\n\n"
                "</details><!-- <details> -->\n\n"
            )
        elif variant in {"summary-comment-close", "summary-comment-open"}:
            lookalike = "</details>" if variant.endswith("close") else "<details>"
            block = f"<details><summary>Real <!-- {lookalike} --> disclosure</summary>\n\n"
        elif variant == "summary-comment-tags":
            block = (
                "<details><!-- <summary>Literal</summary> -->\n"
                "<summary>Real disclosure</summary>\n\n"
            )
        elif variant == "benign-comment-boundaries":
            block = (
                "<details><!-- harmless --><summary>Real</summary><!-- also harmless -->\n\n"
                "Body.\n\n</details>\n\n"
            )
        else:
            literal = "<details><!-- </details> -->\n</details>\n"
            block = (
                "```text\n" + literal + "```\n\n"
                if form == "fenced-literal"
                else "<pre>\n" + literal + "</pre>\n\n"
            )
    elif kind.startswith("html-"):
        opener, closer = {
            "html-comment": ("<!--", "-->"),
            "html-pre": ("<pre>", "</pre>"),
            "html-script": ("<script>", "</script>"),
            "html-style": ("<style>", "</style>"),
            "html-cdata": ("<![CDATA[", "]]>"),
            "html-instruction": ("<?instruction", "?>"),
            "html-declaration": ("<!DOCTYPE ", ">"),
            "html-textarea": ("<textarea>", "</textarea>"),
            "html-generic": ("<div>", "</div>"),
            "html-pre-attribute": ('<pre title="quoted > text">', "</pre>"),
            "html-pre-open-attribute": ('<pre title="', "</pre>"),
        }[kind]
        block = opener + "\n" + "x" * amount + "\n"
        if form == "literal-markers":
            block += "```text\n| Literal | not a table |\n| --- | --- |\n~~~python\n"
        if form != "unclosed":
            block += closer + "\n\n"
        if form.startswith("closing-tail-"):
            block = opener + ("\nharmless\n" if form == "closing-tail-multiline" else "harmless")
            block += closer
            if form == "closing-tail-whitespace":
                block += " \t\r\n\n"
            elif form == "closing-tail-plain":
                block += " ordinary prose.\n\n"
            else:
                following, terminator = {
                    "closing-tail-cdata": ("<![CDATA[", "]]>"),
                    "closing-tail-instruction": ("<?instruction", "?>"),
                }.get(form, ("<!--", "-->"))
                block += " " + following + "\n"
                block += "literal comment" if form == "closing-tail-short" else "x" * 130000
                block += "\n" + terminator + "\n\n"
        target = opener + "\n"
    elif kind == "inline-comment":
        block = "Use <!--" + "x" * amount + "--> then ordinary prose.\n\n"
        target = ""
    elif kind.startswith("paragraph-"):
        first = "Paragraph first line" + ("x" * amount if form == "oversized" else "")
        target = first + "\n"
        indentation = "\t" if kind == "paragraph-tab" else "    "
        block = target + indentation + "indented lazy continuation\n\n"
    elif kind == "indented-code":
        block = "    " + "x" * amount + "\n"
        target = block
        block += ("\n    Another literal code line\n" if form == "blank-lines" else "") + "\n"
    else:
        heading = "Heading text" + ("x" * amount if form == "oversized" else "")
        if form == "multiline":
            heading = "Heading first line\nHeading second line"
        target = heading + "\n"
        block = target + ("---" if kind == "setext-dash" else "===") + "\n\n"
    if form in {"boundary", "multiline", "literal-markers"}:
        prefix = "p" * (capacity - len(target) - 2) + "\n\n"
        assert len(prefix + target) == capacity
    if form == "table-boundary":
        earlier = "| Earlier | Reason |\n| --- | --- |\n| item | retained |\n"
        block = block.removesuffix("\n")
        prefix = "p" * (capacity - len(earlier + block) - 2) + "\n\n" + earlier
        assert len(prefix + block) == capacity
    short = form in {"short", "closing-tail-short"} or form.startswith("short-")
    tail = "After.\n\n" + ("" if short else "z" * 70000 + "\n\n")
    if indented_disclosure and "inside" in form:
        tail += "</details>\n\n"
    if commented_disclosure and form.removeprefix("short-") in {
        "open-comment-close",
        "summary-comment-close",
        "summary-comment-open",
        "summary-comment-tags",
    }:
        tail += "</details>\n\n"
    if form in {
        "closing-tail-context",
        "closing-tail-cdata",
        "closing-tail-instruction",
        "closing-tail-multiline",
    }:
        tail = "After.\n\n"
    if form == "table-boundary":
        tail = "| plain | following |\n\n" + tail
    review = prefix + block + tail + composed
    original = parser.parse(review)
    expected_tables = 0 if form == "unclosed" else 2 if form == "table-boundary" else 1
    assert sum(token.type == "table_open" for token in original) == expected_tables
    if indented_disclosure or commented_disclosure:
        original_context = FooterContext()
        original_context.feed(parser.render(review))
        original_context.close()
        assert original_context.details_depth == original_context.raw_depth == 0
        composed_context = FooterContext()
        composed_context.feed(parser.render(composed))
        composed_context.close()
        if commented_disclosure:
            assert original_context.details_openings == (
                composed_context.details_openings + int(form != "fenced-literal")
            )
            assert bool(original_context.comment_tag_lookalikes) == (
                form not in {"benign-comment-boundaries", "fenced-literal"}
            ), "HTMLParser must distinguish actual comments from visible tags and fenced text"
        else:
            assert original_context.details_openings == (
                composed_context.details_openings + int("inside" in form)
            )
            if not literal_control:
                assert [token.content for token in original if token.type == "code_block"] == [
                    literal + "\n"
                ], "The disclosure-looking tag must independently parse as literal code"
    elif kind.startswith("paragraph-"):
        assert any(
            token.type == "inline"
            and "Paragraph first line" in token.content
            and "indented lazy continuation" in token.content
            for token in original
        )
        assert not any(token.type == "code_block" for token in original)
    elif kind == "indented-code":
        assert len([token for token in original if token.type == "code_block"]) == 1
    elif kind.startswith("setext-"):
        assert any(
            token.type == "heading_open" and token.markup in {"-", "="} for token in original
        )
    elif kind.startswith("html-"):
        assert any(
            token.type == "html_block" and token.content.startswith(opener) for token in original
        )
    assert (len(review.encode("utf-8")) < 60000) == short
    if form in {"oversized", "unclosed", "unsupported"} or (
        form.startswith("closing-tail-")
        and form not in {"closing-tail-short", "closing-tail-whitespace"}
    ):
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
        )
        with pytest.raises(ValueError, match=r"Polly review .* (capacity|multipart comments)"):
            _published_comments(review, review_path, receipt)
        assert not posts and not receipt.exists()
        assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
        return
    requires_refusal = (
        (indented_disclosure or (commented_disclosure and form != "benign-comment-boundaries"))
        and not short
        and not literal_control
    )
    if indented_disclosure or commented_disclosure:
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
        )
        message = (
            "Polly review commented disclosure tags are not supported in multipart comments."
            if commented_disclosure
            else "Polly review indented disclosure tags are not supported in multipart comments."
        )
        try:
            comments = _published_comments(review, review_path, receipt)
        except ValueError as error:
            assert not short and not literal_control
            assert requires_refusal
            assert str(error) == message
            assert not posts and not receipt.exists()
            assert sorted(path.name for path in tmp_path.iterdir()) == ["review.md"]
            with pytest.raises(ValueError, match=re.escape(message)):
                _HELPERS["_markdown_context"](review, continuing=False)
            return
        assert posts == [*comments[1:], comments[0]]
        assert receipt.read_text() == metadata["head_sha"] + "\n"
    else:
        comments = _HELPERS["review_comments"](review, **metadata)
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    if not short or indented_disclosure or commented_disclosure:
        for comment in comments:
            context = FooterContext()
            context.feed(parser.render(comment))
            context.close()
            assert context.footers == 1, "Footer is hidden inside an HTML comment"
            assert context.raw_depth == 0
            assert context.details_depth == 0, "A comment leaves a disclosure open"
    if requires_refusal:
        pytest.fail(
            "Commented disclosure tags must refuse before multipart publication"
            if commented_disclosure
            else "Indented disclosure tags must refuse before multipart publication"
        )
    if short:
        assert len(comments) == 1
        assert comments[0].split("Polly AI Review\n\n", 1)[1].rsplit("\n---\n", 1)[0] == review
        return
    fragments = [_multipart_content(comment) for comment in comments]
    assert len(comments) > 1
    assert "".join(fragments) == review
    rendered = [parser.parse(comment) for comment in comments]
    if form == "closing-tail-whitespace":
        html_prefix = block.split("\n", 1)[0].rstrip(" \t\r")
        expected_raw = [
            token.content
            for token in original
            if token.type == "html_block" and token.content.startswith(html_prefix)
        ]
        assert len(expected_raw) == 1
        assert [
            token.content
            for tokens in rendered
            for token in tokens
            if token.type == "html_block" and token.content.startswith(html_prefix)
        ] == expected_raw
    assert (
        sum(token.type == "table_open" for tokens in rendered for token in tokens)
        == expected_tables
    )
    assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
    row = next(line for line in _REVIEW.splitlines() if line.startswith("| tests/"))
    assert sum(comment.count(row) for comment in comments) == 1
    if indented_disclosure or commented_disclosure:
        for token_type in ("fence", "code_block", "html_block"):
            expected = [token.content for token in original if token.type == token_type]
            actual = [
                token.content
                for fragment in fragments
                for token in parser.parse(fragment)
                if token.type == token_type
            ]
            assert actual == expected
    elif kind.startswith("paragraph-"):
        assert not any(
            token.type in {"code_block", "fence"} for tokens in rendered for token in tokens
        )
        assert [
            token.content
            for tokens in rendered
            for token in tokens
            if token.type == "inline" and "Paragraph first line" in token.content
        ] == [
            token.content
            for token in original
            if token.type == "inline" and "Paragraph first line" in token.content
        ]
    elif kind == "indented-code":
        assert [
            token.content for tokens in rendered for token in tokens if token.type == "code_block"
        ] == [token.content for token in original if token.type == "code_block"]
    elif kind.startswith("setext-"):
        assert [
            (token.tag, tokens[index + 1].content)
            for tokens in rendered
            for index, token in enumerate(tokens)
            if token.type == "heading_open" and token.markup in {"-", "="}
        ] == [
            (token.tag, original[index + 1].content)
            for index, token in enumerate(original)
            if token.type == "heading_open" and token.markup in {"-", "="}
        ]
    else:
        assert [
            token.content
            for tokens in rendered
            for token in tokens
            if token.type == "html_block" and token.content.startswith(opener + "\n")
        ] == [
            token.content
            for token in original
            if token.type == "html_block" and token.content.startswith(opener + "\n")
        ]

    if form in {"boundary", "multiline", "literal-markers"}:
        assert fragments[0] == prefix, "The calibrated dependency must move as a whole"
    if form == "table-boundary":
        assert fragments[0] == prefix + block
        assert [
            token.content
            for tokens in rendered
            for index, token in enumerate(tokens)
            if token.type == "inline" and tokens[index - 1].type == "th_open"
        ].count("Earlier") == 1, "A raw block must not carry a prior table header forward"
    assert sum(block.rstrip("\n") in fragment for fragment in fragments) == 1


@pytest.mark.parametrize(
    ("fence", "literal", "shape"),
    [
        ("```", "```", "line-end"),
        ("~~~", "~~~", "line-end"),
        ("`````", "`````", "line-end"),
        ("~~~~~", "~~~~~", "line-end"),
        ("```", "`````", "line-end"),
        ("~~~", "~~~~~", "line-end"),
        ("```", "`" * 10000, "inline-prose"),
        ("```", "``` ```", "overlap"),
        ("```", "```", "leading-run"),
        ("~~~", "~" * 10000, "inline-prose"),
        ("~~~", "~~~ ~~~", "overlap"),
        ("~~~", "~~~", "leading-run"),
        ("", "`" * 130000, "outside-oversized-run"),
        ("", "~" * 130000, "outside-oversized-run"),
        ("", "```", "outside-short"),
        ("", "~~~", "outside-short"),
        ("", "```", "outside-whitespace"),
        ("", "~~~", "outside-whitespace"),
        ("", "`" * 10000, "outside-short"),
        ("", "~" * 10000, "outside-short"),
        ("", "``` ```", "outside-short"),
        ("", "~~~ ~~~", "outside-short"),
        ("", "```", "outside-oversized-guard"),
        ("", "~~~", "outside-oversized-guard"),
        ("", "```", "outside-oversized-header"),
        ("", "~~~", "outside-oversized-header"),
        ("", "```x``` is inline text", "invalid-info-table"),
        ("", "```", "invalid-info-safe-cut"),
        ("", "`" * 10000, "invalid-info-safe-cut"),
        ("", "```", "invalid-info-unsafe-cut"),
        ("", "```", "invalid-info-oversized"),
        ("~~~", "~~~", "tilde-info-backtick"),
        ("```", "\u00a0", "closing-literal"),
        ("~~~", "\u00a0", "closing-literal"),
        ("```", "\f", "closing-literal"),
        ("~~~", "\f", "closing-literal"),
        ("```", " \t", "closing-ascii"),
        ("~~~", " \t", "closing-ascii"),
        ("```", " \t", "closing-crlf"),
        ("~~~", " \t", "closing-crlf"),
        ("```", "\u00a0", "closing-tail-cut"),
        ("~~~", "\u00a0", "closing-tail-cut"),
        ("```", "\u00a0", "closing-near-tail"),
        ("~~~", "\u00a0", "closing-near-tail"),
        ("```", "\u00a0", "closing-oversized-tail"),
        ("~~~", "\u00a0", "closing-oversized-tail"),
        ("```", "- ", "container"),
        ("~~~", "- ", "container"),
        ("```", "1. ", "container"),
        ("~~~", "1. ", "container"),
        ("```", "2) ", "container"),
        ("~~~", "2) ", "container"),
        ("```", "- - ", "container"),
        ("~~~", "- - ", "container"),
        ("```", "> ", "container"),
        ("~~~", "> ", "container"),
        ("```", "> - ", "container"),
        ("~~~", "> - ", "container"),
        ("```", ">> ", "container"),
        ("~~~", ">> ", "container"),
        ("````", "- ```python\n  x = 1\n  ```\n", "container-literal"),
        ("", "- Finding with ```text after prose.\n\n", "container-prose"),
        ("~~~~", "- ~~~python\n  x = 1\n  ~~~\n", "container-literal"),
        ("", "- Finding with ~~~text after prose.\n\n", "container-prose"),
        ("```", "- ", "container-cut"),
        ("~~~", "- ", "container-cut"),
        ("```", "  ", "container-continuation"),
        ("~~~", "  ", "container-continuation"),
        ("```", "    ", "container-continuation"),
        ("~~~", "    ", "container-continuation"),
        ("```", "- ", "short-fenced-example"),
        ("~~~", "- ", "short-fenced-example"),
        ("```", "", "numbered-lines"),
        ("~~~", "", "numbered-lines"),
        ("`````", "", "numbered-lines"),
        ("~~~~~", "", "numbered-lines"),
    ],
    ids=[
        "backticks",
        "tildes",
        "long-backticks",
        "long-tildes",
        "longer-backticks",
        "longer-tildes",
        "inline-prose-backticks",
        "overlap-backticks",
        "leading-run-backticks",
        "inline-prose-tildes",
        "overlap-tildes",
        "leading-run-tildes",
        "outside-oversized-backticks",
        "outside-oversized-tildes",
        "outside-short-backticks",
        "outside-short-tildes",
        "outside-whitespace-backticks",
        "outside-whitespace-tildes",
        "outside-long-backticks",
        "outside-long-tildes",
        "outside-overlap-backticks",
        "outside-overlap-tildes",
        "outside-oversized-guard-backticks",
        "outside-oversized-guard-tildes",
        "outside-header-overlap-backticks",
        "outside-header-overlap-tildes",
        "invalid-backtick-info-table",
        "invalid-backtick-info-safe-cut",
        "invalid-long-backtick-info-safe-cut",
        "invalid-backtick-info-unsafe-cut",
        "invalid-backtick-info-oversized",
        "tilde-info-backtick",
        "closing-nbsp-backticks",
        "closing-nbsp-tildes",
        "closing-formfeed-backticks",
        "closing-formfeed-tildes",
        "closing-ascii-backticks",
        "closing-ascii-tildes",
        "closing-crlf-backticks",
        "closing-crlf-tildes",
        "closing-calibrated-tail-backticks",
        "closing-calibrated-tail-tildes",
        "closing-near-nbsp-backticks",
        "closing-near-nbsp-tildes",
        "closing-oversized-tail-backticks",
        "closing-oversized-tail-tildes",
        "container-list-backticks",
        "container-list-tildes",
        "container-numbered-dot-backticks",
        "container-numbered-dot-tildes",
        "container-numbered-paren-backticks",
        "container-numbered-paren-tildes",
        "container-nested-list-backticks",
        "container-nested-list-tildes",
        "container-quote-backticks",
        "container-quote-tildes",
        "container-quote-list-backticks",
        "container-quote-list-tildes",
        "container-nested-quote-backticks",
        "container-nested-quote-tildes",
        "container-literal-backticks",
        "container-prose-backticks",
        "container-literal-tildes",
        "container-prose-tildes",
        "container-created-by-cut-backticks",
        "container-created-by-cut-tildes",
        "container-continuation-list-backticks",
        "container-continuation-list-tildes",
        "container-continuation-nested-backticks",
        "container-continuation-nested-tildes",
        "short-fenced-example-backticks",
        "short-fenced-example-tildes",
        "numbered-lines-backticks",
        "numbered-lines-tildes",
        "numbered-lines-long-backticks",
        "numbered-lines-long-tildes",
    ],
)
def test_fence_continuations_preserve_literal_content_and_footer_context(
    fence: str,
    literal: str,
    shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shape == "short-fenced-example":
        from markdown_it import MarkdownIt

        example = (
            literal
            + fence
            + "text\n"
            + "".join(
                "  " + line + "\n"
                for line in [
                    "| Test / case | Behavior protected | Layer | Needed? | Action / rationale |",
                    "| --- | --- | --- | --- | --- |",
                    "| example | | Unit | Keep | |",
                ]
            )
            + "  "
            + fence
            + "\n\n"
        )
        review = _HELPERS["compose_review"](
            example + _REVIEW,
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        parser = MarkdownIt("commonmark").enable("table")
        original = parser.parse(review)
        assert len([token for token in original if token.type == "fence"]) == 1
        assert "| example | | Unit | Keep | |" in next(
            token.content for token in original if token.type == "fence"
        )
        assert sum(token.type == "table_open" for token in original) == 1
        expected = (
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "a" * 40 + " -->\n"
            "<!-- polly-review-run:10-1 -->\n"
            '## <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/'
            'docs/images/omnigent-logo.svg" alt="" height="20" valign="middle" /> '
            "Polly AI Review\n\n"
            + review
            + "\n---\n<sub>Automated review by Polly · [workflow run](https://example.test/run)</sub>\n"
        )
        assert len(expected.encode("utf-8")) < _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha="a" * 40, run_url="https://example.test/run"
        )
        comments = _published_comments(review, review_path, receipt)
        assert comments == posts == [expected]
        assert receipt.read_text() == "a" * 40 + "\n"
        assert not list(tmp_path.glob("comment-part-*.md"))
        rendered = parser.parse(comments[0])
        assert sum(token.type == "table_open" for token in rendered) == 1
        assert [token.content for token in rendered if token.type == "fence"] == [
            token.content for token in original if token.type == "fence"
        ]
        return
    if shape == "container-continuation":
        from markdown_it import MarkdownIt

        metadata = {
            "head_sha": "a" * 40,
            "run_id": "10-1",
            "run_url": "https://example.test/run",
        }
        composed = _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
        lead = "- item\n\n" + ("  - inner\n\n" if len(literal) == 4 else "")
        lead += literal + fence + "python\n"
        closing = literal + fence + "\n\n"
        line = "literal code line\n"
        short_review = lead + literal + line + closing + composed
        single = _HELPERS["review_comments"](short_review, **metadata)
        assert len(single) == 1 and short_review in single[0]
        review = lead + (literal + line) * 4000 + closing + composed
        parser = MarkdownIt("commonmark").enable("table")
        original = parser.parse(review)
        assert [token.content for token in original if token.type == "fence"] == [line * 4000]
        assert sum(token.type == "table_open" for token in original) == 1
        assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        assert len(review.encode("utf-8")) > 60000
        for token in parser.parse(single[0]):
            if token.type in {"fence", "code_block"}:
                assert "<sub>Automated review by Polly" not in token.content
                assert "tests/test_parser.py::test_empty" not in token.content
        review_path, receipt, posts = _prepare_publication(
            tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
        )
        with pytest.raises(ValueError) as error:
            _published_comments(review, review_path, receipt)
        assert (
            str(error.value)
            == "Polly review container fences are not supported in multipart comments."
        )
        assert not posts and not receipt.exists()
        assert not list(tmp_path.glob("comment*.md"))
        return
    if shape.startswith("container"):
        from markdown_it import MarkdownIt

        metadata = {
            "head_sha": "a" * 40,
            "run_id": "10-1",
            "run_url": "https://example.test/run",
        }
        composed = _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
        if shape == "container-cut":
            lead = "Prose " + literal + fence + "python\n\n"
        elif shape == "container":
            continuation = {
                "- ": "  ",
                "1. ": "   ",
                "2) ": "   ",
                "- - ": "    ",
                "> ": "> ",
                "> - ": ">   ",
                ">> ": ">> ",
            }[literal]
            lead = literal + fence + "python\n" + continuation + "x = 1\n"
            lead += continuation + fence + "\n\n"
        elif shape == "container-literal":
            lead = fence + "text\n" + literal + fence + "\n\n"
        else:
            lead = literal
        parser = MarkdownIt("commonmark").enable("table")
        short_review = lead + composed
        single = _HELPERS["review_comments"](short_review, **metadata)
        assert len(single) == 1 and short_review in single[0]
        assert "part 1 appears." not in single[0]
        for token in parser.parse(single[0]):
            if token.type in {"fence", "code_block"}:
                assert "<sub>Automated review by Polly" not in token.content
                assert "tests/test_parser.py::test_empty" not in token.content
        review = lead + "Prose finding line.\n" * 4000 + composed
        if shape == "container-cut":
            calibration = _HELPERS["review_comments"]("x" * 130000 + "\n\n" + composed, **metadata)
            content = calibration[0].split("part 1 appears.\n\n", 1)[1].rsplit("\n\n---\n", 1)[0]
            review = (
                "x" * (len(content) - len(literal))
                + literal
                + fence
                + "python\n\n"
                + "Prose finding line.\n" * 4000
                + composed
            )
            raw_cut = _HELPERS["split_review"](review, len(content), split_once=True)[0]
            assert literal + fence not in raw_cut
        original = parser.parse(review)
        code = [token.content for token in original if token.type == "fence"]
        assert code == (
            ["x = 1\n"]
            if shape == "container"
            else [literal]
            if shape == "container-literal"
            else []
        )
        assert sum(token.type == "table_open" for token in original) == 1
        assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        assert len(review.encode("utf-8")) > 60000
        if shape in {"container", "container-cut"}:
            review_path, receipt, posts = _prepare_publication(
                tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
            )
            with pytest.raises(ValueError) as error:
                _published_comments(review, review_path, receipt)
            assert (
                str(error.value)
                == "Polly review container fences are not supported in multipart comments."
            )
            assert not posts and not receipt.exists()
            assert not list(tmp_path.glob("comment*.md"))
            return
        comments = _HELPERS["review_comments"](review, **metadata)
        assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
        assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
        rendered = [parser.parse(comment) for comment in comments]
        for tokens in rendered:
            for token in tokens:
                if token.type in {"fence", "code_block"}:
                    assert "<sub>Automated review by Polly" not in token.content, (
                        "Publication footer is inside independently rendered code"
                    )
                    assert "Prose finding line." not in token.content, (
                        "Findings are inside independently rendered code"
                    )
                    assert "tests/test_parser.py::test_empty" not in token.content
        assert sum(token.type == "table_open" for tokens in rendered for token in tokens) == 1
        assert [
            token.content
            for tokens in rendered
            for i, token in enumerate(tokens)
            if token.type == "inline" and tokens[i - 1].type == "th_open"
        ] == ["Test / case", "Behavior protected", "Layer", "Needed?", "Action / rationale"]
        assert [
            token.content for tokens in rendered for token in tokens if token.type == "fence"
        ] == code
        fragments = []
        for comment in comments:
            _, marker, content = comment.partition("part 1 appears.\n\n")
            assert marker
            content, separator, footer = content.rpartition("\n\n---\n")
            assert separator and footer.startswith("<sub>Automated review by Polly")
            fragments.append(content)
        assert "".join(fragments) == review
        assert sum(comment.count("Prose finding line.") for comment in comments) == 4000
        row = next(line for line in _REVIEW.splitlines() if line.startswith("| tests/"))
        assert sum(comment.count(row) for comment in comments) == 1
        assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
        return
    if shape.startswith(("outside-", "invalid-info-", "closing-")):
        from markdown_it import MarkdownIt

        metadata = {
            "head_sha": "a" * 40,
            "run_id": "10-1",
            "run_url": "https://example.test/run",
        }
        composed = _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
        if shape.startswith("closing-"):
            newline = "\r\n" if shape == "closing-crlf" else "\n"
            opening = fence + "text" + newline
            code = "x" * 70000 + newline
            if shape == "closing-tail-cut":
                calibration = _HELPERS["review_comments"](
                    opening + "x" * 130000 + "\n" + fence + "\nAfter.\n\n" + composed,
                    **metadata,
                )
                capacity = max(
                    len(
                        _multipart_content(comment)
                        .removeprefix(opening)
                        .removesuffix("\n" + fence + "\n")
                    )
                    for comment in calibration
                )
                line = fence + " " * (capacity - len(fence)) + literal
                raw_cut = _HELPERS["split_review"](line, capacity, split_once=True)[0]
                assert len(raw_cut) == capacity and literal not in raw_cut
                assert raw_cut.startswith(fence) and not raw_cut[len(fence) :].strip(" \t")
                code += line + newline
            elif shape == "closing-oversized-tail":
                code += fence + " " * 130000 + literal + newline
            elif shape == "closing-near-tail":
                code += fence + literal + " " * 130000 + "a" + newline
            elif shape == "closing-literal":
                code += fence + literal + newline
            code += "y" * 70000 + newline
            tail = literal if shape in {"closing-ascii", "closing-crlf"} else ""
            review = opening + code + fence + tail + newline + "After.\n\n" + composed
            parser = MarkdownIt("commonmark").enable("table")
            original = parser.parse(review)
            original_code = [token.content for token in original if token.type == "fence"]
            assert original_code == [code.replace("\r\n", "\n")]
            assert sum(token.type == "table_open" for token in original) == 1
            if shape in {"closing-tail-cut", "closing-oversized-tail"}:
                review_path, receipt, posts = _prepare_publication(
                    tmp_path,
                    monkeypatch,
                    head_sha=metadata["head_sha"],
                    run_url=metadata["run_url"],
                )
                try:
                    comments = _published_comments(review, review_path, receipt)
                except ValueError as error:
                    assert shape in {"closing-tail-cut", "closing-oversized-tail"}
                    assert (
                        str(error)
                        == "Polly review literal fence run cannot fit within comment capacity."
                    )
                    assert not posts and not receipt.exists()
                    assert not list(tmp_path.glob("comment*.md"))
                    return
            else:
                comments = _HELPERS["review_comments"](review, **metadata)
            assert shape not in {"closing-tail-cut", "closing-oversized-tail"}
            assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
            assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
            assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
            rendered = [parser.parse(comment) for comment in comments]
            for tokens in rendered:
                for token in tokens:
                    if token.type in {"fence", "code_block"}:
                        assert "<sub>Automated review by Polly" not in token.content, (
                            "Publication footer is inside independently rendered code"
                        )
                        assert "After." not in token.content
                        assert "tests/test_parser.py::test_empty" not in token.content
            remaining = review
            remaining_code = original_code[0]
            reconstructed = ""
            table_count = 0
            for number, (comment, tokens) in enumerate(zip(comments, rendered, strict=True)):
                for token in tokens:
                    if token.type == "fence":
                        fragment = token.content
                        if not remaining_code.startswith(fragment):
                            assert fragment.endswith("\n")
                            assert not fragment.endswith("\n\n")
                            fragment = fragment[:-1]
                        assert remaining_code.startswith(fragment)
                        remaining_code = remaining_code[len(fragment) :]
                table_count += sum(token.type == "table_open" for token in tokens)
                if any(token.type == "table_open" for token in tokens):
                    assert [
                        token.content
                        for index, token in enumerate(tokens)
                        if token.type == "inline" and tokens[index - 1].type == "th_open"
                    ] == [
                        "Test / case",
                        "Behavior protected",
                        "Layer",
                        "Needed?",
                        "Action / rationale",
                    ]
                fragment = _multipart_content(comment)
                if number and not remaining.startswith(fragment) and fragment.startswith(opening):
                    fragment = fragment[len(opening) :]
                if not remaining.startswith(fragment):
                    suffix = fence + "\n"
                    assert fragment.endswith(suffix)
                    fragment = fragment[: -len(suffix)]
                    if not remaining.startswith(fragment):
                        assert fragment.endswith("\n")
                        assert not fragment.endswith("\n\n")
                        fragment = fragment[:-1]
                assert remaining.startswith(fragment)
                remaining = remaining[len(fragment) :]
                reconstructed += fragment
            assert not remaining_code
            assert not remaining and reconstructed == review
            assert table_count == 1
            assert sum(comment.count("After.") for comment in comments) == 1
            assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
            return
        if shape.startswith("invalid-info-"):
            row = next(line for line in _REVIEW.splitlines() if line.startswith("| tests/"))
            rows = [
                f"| tests/test_parser.py::test_case[{number}] | Input {number} | Unit | Keep | "
                + "r" * 150
                + " |"
                for number in range(400)
            ]
            composed = _HELPERS["compose_review"](
                _REVIEW.replace(row, "\n".join(rows)),
                {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
            )
            if shape == "invalid-info-table":
                prose = literal + "\n\nAfter.\n"
            else:
                calibration = _HELPERS["review_comments"](
                    "x" * 130000 + "\n\n" + composed, **metadata
                )
                boundary = len(_multipart_content(calibration[0]))
                invalidator = (
                    130000
                    if shape == "invalid-info-oversized"
                    else boundary + 20
                    if shape == "invalid-info-unsafe-cut"
                    else boundary - 20
                )
                opener = ("   " if shape == "invalid-info-safe-cut" else "") + literal
                prose = opener + "x" * (invalidator - len(opener)) + "` info\n\nAfter.\n"
                assert prose.index("`", len(opener)) == invalidator
                assert (invalidator < boundary) == (shape == "invalid-info-safe-cut")
                prose += "y" * 70000 + "\n\n"
        elif shape == "outside-oversized-run":
            prose = "Before.\n\nx" + literal + "a\n\nAfter.\n"
        elif shape == "outside-oversized-guard":
            prose = "Before.\n\nx" + " " * 130000 + literal + "info\n\nAfter.\n"
        elif shape == "outside-oversized-header":
            prose = (
                "| x"
                + literal
                + " | "
                + "h" * 70000
                + literal
                + " |\n| --- | --- |\n| item | note |\n\nAfter.\n"
            )
        else:
            calibration = _HELPERS["review_comments"]("x" * 130000 + "\n\n" + composed, **metadata)
            boundary = len(_multipart_content(calibration[0]))
            spaces = 200 if shape == "outside-whitespace" else 0
            prose = (
                "x" * (boundary - spaces)
                + " " * spaces
                + literal
                + "info\n\n"
                + "y" * 70000
                + "\n\nAfter.\n"
            )
        review = prose + "\n" + composed
        parser = MarkdownIt("commonmark").enable("table")
        original = parser.parse(review)
        assert not any(token.type == "fence" for token in original)
        if shape.startswith("invalid-info-"):
            assert sum(token.type == "table_open" for token in original) == 1
        if shape.startswith("outside-oversized-") or shape in {
            "invalid-info-unsafe-cut",
            "invalid-info-oversized",
        }:
            review_path, receipt, posts = _prepare_publication(
                tmp_path, monkeypatch, head_sha=metadata["head_sha"], run_url=metadata["run_url"]
            )
            try:
                comments = _published_comments(review, review_path, receipt)
            except ValueError as error:
                if shape.startswith("invalid-info-"):
                    assert shape in {"invalid-info-unsafe-cut", "invalid-info-oversized"}
                else:
                    assert shape.startswith("outside-oversized-")
                kind = (
                    "table header" if shape == "outside-oversized-header" else "literal fence run"
                )
                assert str(error) == f"Polly review {kind} cannot fit within comment capacity."
                assert not posts
                assert not receipt.exists()
                assert not list(tmp_path.glob("comment*.md"))
                return
        else:
            comments = _HELPERS["review_comments"](review, **metadata)
        assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
        assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
        assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
        table_count = 0
        raw_fragments = []
        for comment in comments:
            tokens = parser.parse(comment)
            for token in tokens:
                if token.type in {"fence", "code_block"}:
                    assert "<sub>Automated review by Polly" not in token.content, (
                        "Publication footer is inside independently rendered code"
                    )
                    assert "tests/test_parser.py::test_empty" not in token.content, (
                        "Assessment is inside independently rendered code"
                    )
            assert not any(token.type == "fence" for token in tokens), (
                "Literal prose became an opening fence"
            )
            if (
                shape.startswith("invalid-info-")
                and "| tests/test_parser.py::test_case[" in comment
            ):
                assert sum(token.type == "table_open" for token in tokens) == 1
                assert [
                    token.content
                    for index, token in enumerate(tokens)
                    if token.type == "inline" and tokens[index - 1].type == "th_open"
                ] == [
                    "Test / case",
                    "Behavior protected",
                    "Layer",
                    "Needed?",
                    "Action / rationale",
                ]
            table_count += sum(token.type == "table_open" for token in tokens)
            raw_fragments.append(_multipart_content(comment))
        assert not shape.startswith("outside-oversized-")
        if shape.startswith("invalid-info-"):
            header, delimiter = _REVIEW.split("### Tests\n", 1)[1].splitlines()[:2]
            continued_header = header + "\n" + delimiter + "\n"
            continued_details = "<details>\n<summary>Review continued</summary>\n\n"
            remaining = review
            for number, fragment in enumerate(raw_fragments):
                if fragment.startswith(continued_details):
                    assert number > 0
                    fragment = fragment[len(continued_details) :]
                if fragment.lstrip().startswith("|") and not remaining.startswith(fragment):
                    assert fragment.startswith(continued_header)
                    fragment = fragment[len(continued_header) :]
                if not remaining.startswith(fragment):
                    assert fragment.endswith("\n\n</details>")
                    fragment = fragment.removesuffix("\n\n</details>")
                assert remaining.startswith(fragment)
                remaining = remaining[len(fragment) :]
                raw_fragments[number] = fragment
            assert not remaining
            assert table_count >= 2
            for row in rows:
                assert sum(comment.count(row) for comment in comments) == 1
        else:
            assert table_count == sum(token.type == "table_open" for token in original) == 1
            assert "".join(_multipart_content(comment) for comment in comments) == review
            row = next(line for line in _REVIEW.splitlines() if line.startswith("| tests/"))
            assert sum(comment.count(row) for comment in comments) == 1
        assert "".join(raw_fragments) == review
        assert sum(comment.count("After.") for comment in comments) == 1
        assert sum(comment.count("<!-- polly-reviewed-sha:") for comment in comments) == 1
        return
    if shape == "numbered-lines":
        code = "".join(
            f"L{number:05d}\n" + ("\n" if number % 1000 == 999 else "") for number in range(19000)
        )
    elif shape == "leading-run":
        code = literal + " " * 58000 + "a" + "\n" + "y" * 70000 + "\n"
    else:
        code = (
            "x" * 59471
            + literal
            + ("" if shape == "line-end" else "a")
            + "\n"
            + "y" * 70000
            + "\n"
        )
    info = "x`info" if shape == "tilde-info-backtick" else "text"
    review = fence + info + "\n" + code + fence + "\nAfter."
    if shape == "numbered-lines":
        review += "\n\n" + _HELPERS["compose_review"](
            _REVIEW, {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]}
        )
    comments = _HELPERS["review_comments"](
        review, head_sha="a" * 40, run_id="10-1", run_url="https://example.test/run"
    )
    assert 1 < len(comments) <= _HELPERS["MAX_REVIEW_PARTS"]
    assert all(len(comment.encode("utf-8")) <= 60000 for comment in comments)
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark").enable("table")
    assert [token.content for token in parser.parse(review) if token.type == "fence"] == [code]
    fragments = []
    for comment in comments:
        tokens = parser.parse(comment)
        fragments.extend(token.content for token in tokens if token.type == "fence")
        assert not any(
            token.type in {"fence", "code_block"}
            and "<sub>Automated review by Polly" in token.content
            for token in tokens
        ), "Publication footer is inside independently parsed code"
        assert not any(
            token.type in {"fence", "code_block"} and "After." in token.content.split("\n")
            for token in tokens
        ), "Following prose is inside independently parsed code"
        assert (
            sum(
                child.type == "html_inline" and child.content == "<sub>"
                for token in tokens
                for child in token.children or []
            )
            == 1
        ), "Every footer must be independently parsed outside code"
    if shape == "numbered-lines":
        assert "".join(fragments) == code, "Multipart fences must preserve every code newline"
        assert (
            sum(
                token.type == "table_open"
                for comment in comments
                for token in parser.parse(comment)
            )
            == 1
        )
        assert all(fragment.endswith("\n") for fragment in fragments)
    remaining = code
    reconstructed = ""
    for fragment in fragments:
        if not remaining.startswith(fragment):
            assert fragment.endswith("\n")
            assert not fragment.endswith("\n\n")
            fragment = fragment[:-1]  # Only a cut inside a code line needs a newline.
        assert remaining.startswith(fragment)
        reconstructed += fragment
        remaining = remaining[len(fragment) :]
    assert reconstructed == code
    assert not remaining
    assert sum(comment.count("After.") for comment in comments) == 1


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

    assert _HELPERS["COMMENT_BYTE_LIMIT"] == 60000
    empty_comment = _HELPERS["review_comments"]("", **review_metadata)[0]
    prefix, separator, footer = empty_comment.partition("\n---\n")
    assert separator
    for content, refusal in (
        (
            "- Outer assessment\n  - Nested assessment",
            "Polly review nested lists are not supported in multipart comments.",
        ),
        (
            "<div>Assessment details.</div>",
            "Polly review unsupported raw HTML is not supported in multipart comments.",
        ),
    ):
        assessment = _HELPERS["compose_review"](
            _REVIEW.replace("Fix the parser.", "Fix the parser.\n\n" + content + "\n"),
            {"markdown": "Computed counts", "test_files": ["tests/test_parser.py"]},
        )
        assessment += "x" * (60000 - wrapper_bytes - len(assessment.encode("utf-8")))
        assert len(assessment.encode("utf-8")) == 60000 - wrapper_bytes
        exact_comments = _HELPERS["review_comments"](assessment, **review_metadata)
        assert exact_comments == [prefix + assessment + separator + footer]
        assert len(exact_comments[0].encode("utf-8")) == 60000
        overflow_assessment = assessment + "x"
        assert len(overflow_assessment.encode("utf-8")) + wrapper_bytes == 60001
        with pytest.raises(ValueError) as error:
            _HELPERS["review_comments"](overflow_assessment, **review_metadata)
        assert str(error.value) == refusal


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
        "        controls = directory / 'controls.json'\n"
        "        comments = json.loads(controls.read_text()) if controls.exists() else []\n"
        "        comments += [dict(user=dict(type='Bot'), html_url=f'https://example.test/{i}', "
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
    controls = tmp_path / "controls.json"
    lookalikes = [
        {
            "user": {"type": "User"},
            "html_url": "https://example.test/human",
            "body": primary_header + "Human-authored metadata lookalike.",
        },
        {
            "user": {"type": "Bot"},
            "html_url": "https://example.test/unrelated-bot",
            "body": "Unrelated bot quoting an example:\n```text\n" + primary_header + "```\n",
        },
    ]
    controls.write_text(json.dumps(lookalikes))
    assert not already_reviewed()
    assert not (tmp_path / "skip.md").exists()
    skip_notice = {
        "user": {"type": "Bot"},
        "html_url": "https://example.test/existing-skip",
        "body": "<!-- polly-skipped-sha: " + "a" * 40 + " -->\nExisting skip notice.\n",
    }
    controls.write_text(json.dumps([*lookalikes, skip_notice]))
    assert already_reviewed()
    assert not (tmp_path / "skip.md").exists(), "Existing notice must not post another skip"
    controls.write_text(json.dumps(lookalikes))
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
    posts = calls
    comments = [
        (tmp_path / filename).read_text()
        for filename in ["comment.md", "comment-part-2.md", "comment-part-3.md"]
    ]
    assert posts == [*comments[1:], comments[0]]
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
