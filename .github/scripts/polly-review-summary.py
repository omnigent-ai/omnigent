"""Compute diff metrics and enforce the published Polly summary structure."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

TESTS = "Tests and test support"
CATEGORIES = (TESTS, "Documentation", "Dependencies and lockfiles", "Implementation / other")
TEST_FIELD_COUNT = 5
TEST_HEADER = ["test / case", "behavior protected", "layer", "needed?", "action / rationale"]
COMMENT_BYTE_LIMIT = 60000
# The part ceiling is not guaranteed capacity: repeated scans also consume work.
MAX_REVIEW_PARTS = 64
RENDER_WORK_BYTE_LIMIT = COMMENT_BYTE_LIMIT * MAX_REVIEW_PARTS * 16
LINKED_ISSUE_BODY_LIMIT = 4096
LINKED_ISSUES_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    closingIssuesReferences(first: 5) {
      totalCount nodes { number title state body repository { nameWithOwner } }
    }
  } }
}"""


def category(filename: str) -> str:
    path = PurePosixPath(filename)
    if (
        set(path.parts) & {"tests", "test", "__tests__", "__mocks__", "e2e"}
        or path.name.startswith("test_")
        or re.search(r"(?:[._](?:test|spec|e2e))\.[^.]+$", path.name)
    ):
        return TESTS
    if "docs" in path.parts or path.suffix in {".md", ".mdx", ".rst"}:
        return "Documentation"
    if (
        path.name in {"pyproject.toml", "package.json", "pnpm-lock.yaml", "package-lock.json"}
        or path.suffix == ".lock"
        or (path.name.startswith("requirements") and path.suffix in {".txt", ".in"})
    ):
        return "Dependencies and lockfiles"
    return "Implementation / other"


def diff_stats(diff: Path) -> dict:
    # --numstat only inspects the frozen patch; it never applies or executes it.
    raw = (
        subprocess.run(
            ["git", "apply", "--numstat", "-z", "--", str(diff.resolve())],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        if diff.stat().st_size
        else b""
    )
    groups = {name: {"files": 0, "added": 0, "deleted": 0} for name in CATEGORIES}
    test_files = []
    binary_files = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        added, deleted, encoded_path = record.split(b"\t", 2)
        filename = encoded_path.decode("utf-8", errors="replace")
        kind = category(filename)
        group = groups[kind]
        group["files"] += 1
        if kind == TESTS:
            test_files.append(filename)
        if added == b"-" or deleted == b"-":
            binary_files += 1
        else:
            group["added"] += int(added)
            group["deleted"] += int(deleted)
    added = sum(group["added"] for group in groups.values())
    deleted = sum(group["deleted"] for group in groups.values())
    total = added + deleted
    files = sum(group["files"] for group in groups.values())
    lines = [
        f"**Diff size (computed):** {files} files; +{added} / -{deleted} lines "
        f"({total} changed text lines); {binary_files} binary files.",
        "",
        "| Category | Files | Added | Deleted | Share of changed text lines |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, group in groups.items():
        share = f"{100 * (group['added'] + group['deleted']) / total:.1f}%" if total else "N/A"
        lines.append(
            f"| {name} | {group['files']} | {group['added']} | {group['deleted']} | {share} |"
        )
    lines += [
        "",
        "Shares use additions + deletions, including generated text and lockfiles. "
        + "Binary files have no line count. Tests include fixtures/helpers under test paths "
        + "and colocated test/spec files; categories are path-based, not coverage measurements.",
    ]
    return {"markdown": "\n".join(lines), "test_files": test_files}


def collapse_test_assessment(summary_body: str) -> str:
    return re.sub(
        r"(^### Tests[ \t]*\n)(.*?)(?=^#{1,3} |\Z)",
        lambda match: (
            match[1]
            + "\n<details>\n<summary>Test-by-test assessment</summary>\n\n"
            + match[2].strip()
            + "\n\n</details>\n\n"
        ),
        summary_body,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )


def _pipes(body: str, honor_code_spans: bool) -> list[tuple[int, bool]]:
    """Return (index, inside_code_span) for each pipe not immediately preceded by a backslash."""
    pipes = []
    i = 0
    while i < len(body):
        char = body[i]
        escaped = i > 0 and body[i - 1] == "\\"
        if char == "`" and honor_code_spans and not escaped:
            run = re.match(r"`+", body[i:])[0]
            closing = re.compile(rf"(?<!`){run}(?!`)").search(body, i + len(run))
            if closing is None:
                i += len(run)
                continue
            # A pipe preceded by a backslash inside a code span is already escaped.
            pipes.extend(
                (j, True)
                for j in range(i + len(run), closing.start())
                if body[j] == "|" and body[j - 1] != "\\"
            )
            i = closing.end()
            continue
        if char == "|" and not escaped:
            pipes.append((i, False))
        i += 1
    return pipes


def _honors_code_spans(body: str) -> bool:
    """Code spans are honored only when that yields the expected number of test fields."""
    delimiters = [index for index, in_code in _pipes(body, True) if not in_code]
    return len(delimiters) + 1 == TEST_FIELD_COUNT


def split_table_row(body: str) -> list[str]:
    honor = _honors_code_spans(body)
    cells = []
    start = 0
    for index, in_code in _pipes(body, honor):
        if not in_code:
            cells.append(body[start:index])
            start = index + 1
    cells.append(body[start:])
    return cells


def _escape_code_span_pipes(body: str) -> str:
    if not _honors_code_spans(body):
        return body
    pieces = []
    last = 0
    for index, in_code in _pipes(body, True):
        if in_code:
            pieces += [body[last:index], "\\|"]
            last = index + 1
    pieces.append(body[last:])
    return "".join(pieces)


def escape_tests_table(text: str) -> str:
    """Escape pipes inside code spans in table rows, so GitHub renders them as cell text."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|"):
            body = stripped.strip("|")
            body_start = (len(line) - len(line.lstrip())) + (
                len(stripped) - len(stripped.lstrip("|"))
            )
            line = (
                line[:body_start] + _escape_code_span_pipes(body) + line[body_start + len(body) :]
            )
        lines.append(line)
    return "\n".join(lines)


def compose_review(review: str, stats: dict) -> str:
    summaries = list(re.finditer(r"^## Summary\s*$", review, re.MULTILINE))
    if len(summaries) != 1:
        raise ValueError("Incomplete Polly review: expected exactly one '## Summary'.")
    summary = summaries[0]
    body = re.split(r"^## ", review[summary.end() :], maxsplit=1, flags=re.MULTILINE)[0]
    sections = {}
    section_starts = {}
    for name in ("Changes", "Tests", "Scope"):
        matches = list(re.finditer(rf"^### {name}\s*$", body, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"Incomplete Polly review: expected one '### {name}' in Summary.")
        text = re.split(r"^### ", body[matches[0].end() :], maxsplit=1, flags=re.MULTILINE)[0]
        if not text.strip():
            raise ValueError(f"Incomplete Polly review: empty '{name}' assessment.")
        sections[name] = text
        section_starts[name] = summary.end() + matches[0].end()
    rows = []
    if stats["test_files"]:
        header = TEST_HEADER
        header_seen = False
        for line in sections["Tests"].split("\n"):
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in split_table_row(line.strip().strip("|"))]
            if [cell.lower() for cell in cells] == header:
                header_seen = True
                continue
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if len(cells) != TEST_FIELD_COUNT or not all(cells):
                raise ValueError(
                    "Incomplete Polly review: test rows need all five assessment fields."
                )
            verdict = cells[3].strip("*` ").lower()
            if verdict not in {"keep", "consolidate", "move layer", "remove", "uncertain"}:
                raise ValueError(f"Incomplete Polly review: unknown test verdict '{cells[3]}'.")
            rows.append(cells[0].replace(r"\|", "|"))
        if not header_seen:
            raise ValueError("Incomplete Polly review: missing test assessment table header.")
    missing = [path for path in stats["test_files"] if not any(path in row for row in rows)]

    # A quoted case may have explanatory text; plain labels may group cases.
    def is_case_only(row: str) -> bool:
        label = row.strip("* ")
        if not label or label.startswith("["):
            return False
        if label.startswith("`"):
            identifier, separator, _ = label[1:].partition("`")
            if not separator:
                return False
        else:
            identifier = label
        outside_parameters = re.sub(r"\[[^\]]*\]", "", identifier)
        return bool(outside_parameters.strip()) and "/" not in outside_parameters

    # Existing coverage from other files can appear beside the changed file's case-only rows.
    has_case_only = any(is_case_only(row) for row in rows)
    if len(stats["test_files"]) == 1 and has_case_only and missing:
        path = re.escape(missing[0])
        if re.search(rf"(?<![\w./~+-]){path}(?![\w/~+-]|\.[\w-])", sections["Tests"]):
            missing = []
    if missing:
        raise ValueError(f"Incomplete Polly review: unassessed test files: {', '.join(missing)}")
    tests_start = section_starts["Tests"]
    review = (
        review[:tests_start]
        + escape_tests_table(sections["Tests"])
        + review[tests_start + len(sections["Tests"]) :]
    )
    return (
        review[: summary.end()].rstrip()
        + "\n\n"
        + stats["markdown"]
        + "\n\n"
        + collapse_test_assessment(review[summary.end() :].lstrip())
    )


def split_review(
    review: str, byte_limit: int, *, in_table_row: bool = False, split_once: bool = False
) -> list[str]:
    """Partition without dropping text; optionally leave the tail unsplit."""
    if byte_limit < 4:
        raise ValueError("Comment wrappers leave no room for review content.")
    parts = []
    while len(encoded := review.encode("utf-8")) > byte_limit:
        boundary = byte_limit
        while encoded[boundary] & 0xC0 == 0x80:
            boundary -= 1
        prefix = encoded[:boundary].decode("utf-8")
        line_end = prefix.rfind("\n") + 1
        end = line_end or len(prefix)
        if line_end:
            in_table_row = False
            header_start = prefix.rfind("\n", 0, end - 1) + 1
            next_line = review[end:].split("\n", 1)[0]
            if (
                header_start
                and prefix[header_start:end].lstrip().startswith("|")
                and next_line.lstrip().startswith("|")
                and all(
                    re.fullmatch(r"\s*:?-+:?\s*", cell)
                    for cell in split_table_row(next_line.strip().strip("|"))
                )
            ):
                end = header_start
        else:
            in_table_row = in_table_row or review.lstrip().startswith("|")
            if in_table_row and review[end:].split("\n", 1)[0].strip() in {"", "|"}:
                # Keep nonblank rationale text with the terminal delimiter and whitespace.
                content = prefix.rstrip()
                if content.endswith("|") and not content.endswith(r"\|"):
                    content = content[:-1].rstrip()
                end = len(content) - 1
            if review[end - 1 : end + 1] == r"\|":
                end -= 1
        if end <= 0:
            raise ValueError("Polly review split cannot make progress within comment capacity.")
        parts.append(review[:end])
        review = review[end:]
        if split_once:
            break
    parts.append(review)
    return parts


def _disclosure_code_spans(line: str) -> list[tuple[int, int]]:
    """Locate complete same-line backtick spans without rescanning unmatched tails."""
    runs = list(re.finditer(r"`+", line))
    following: dict[int, int] = {}
    pairs: dict[int, tuple[int, int]] = {}
    for index in range(len(runs) - 1, -1, -1):
        run = runs[index]
        start = slash_start = run.start()
        while slash_start and line[slash_start - 1] == "\\":
            slash_start -= 1
        # An escaped first backtick leaves the rest of a run available to open code.
        start += (start - slash_start) % 2
        width = run.end() - start
        if width and width in following:
            pairs[index] = (slash_start, following[width])
        following[len(run[0])] = index
    spans = []
    index = 0
    previous_end = 0
    while index < len(runs):
        if index in pairs:
            start, closer = pairs[index]
            # Raw HTML and link syntax may consume backticks before inline parsing.
            preceding = line[previous_end:start]
            contexts: list[str] = []
            quote = ""
            for token in re.finditer(
                r"\\.|<!--|--!?>|<!\[CDATA\[|\]\]>|<\?|\?>|"
                r"</?[A-Za-z][A-Za-z0-9-]*(?=[ \t\r/>]|$)|<![A-Z]|"
                r"\]\(|\]\[|[()\[\]>\"']",
                preceding,
            ):
                symbol = token[0]
                if symbol.startswith("\\"):
                    continue
                if quote:
                    if symbol == quote:
                        quote = ""
                elif contexts and contexts[-1] in {"-->", "]]>", "?>"}:
                    if symbol == "--!>" and contexts[-1] == "-->":
                        # CommonMark and HTML disagree; later closers cannot remove ambiguity.
                        break
                    if symbol == contexts[-1]:
                        contexts.pop()
                elif contexts and contexts[-1] == ">":
                    if symbol in {'"', "'"}:
                        quote = symbol
                    elif symbol in {">", "--!>"}:
                        contexts.pop()
                elif contexts and contexts[-1] == "]":
                    if symbol == "]":
                        contexts.pop()
                elif symbol.startswith("<"):
                    contexts.append(
                        {"<!--": "-->", "<![CDATA[": "]]>", "<?": "?>"}.get(symbol, ">")
                    )
                elif contexts and contexts[-1] == ")":
                    if symbol in {"(", "]("}:
                        contexts.append(")")
                    elif symbol == ")":
                        contexts.pop()
                    elif symbol in {'"', "'"} and preceding[token.start() - 1 : token.start()] in {
                        " ",
                        "\t",
                    }:
                        quote = symbol
                elif symbol in {"](", "]["}:
                    contexts.append(")" if symbol == "](" else "]")
            if contexts or quote:
                break
            spans.append((start, runs[closer].end()))
            previous_end = runs[closer].end()
            index = closer + 1
        else:
            break
    return spans


def _markdown_context(
    text: str,
    *,
    validate_assessments: bool = False,
    continuing: bool = True,
    protected_spans: list[tuple[int, int, str]] | None = None,
) -> tuple[str, str, str]:
    """Reopen Markdown context and optionally validate final assessment rows.

    Mid-row continuation requires five parsed fields; other widths fail closed.
    Only canonical assessment rows require every field to be populated.
    Disclosure balancing supports lowercase <details>/</details> without attributes
    at the first nonblank position; <summary> may share an opening line.
    Multipart preflight keeps list/quote blocks, including nested lists, indivisible
    and rejects container-prefixed or indented fences outside active fences.
    Tables require leading pipes; disclosure summaries must be canonical single-line
    headers that fit indivisibly. Other forms fail closed before multipart publication.
    Unambiguous same-line disclosure code spans stay literal and indivisible.
    Complete comments, plain raw HTML blocks, indented blocks and setext headings stay
    indivisible; raw closing lines allow only space/tab and structural CR tails in
    multipart comments. Other raw HTML and unclosed literal blocks fail closed.
    Reference-definition-like lines outside fenced/raw blocks fail closed in multipart.
    Destination-bearing words and inline-link blocks remain indivisible at cuts.
    Ambiguous inline-code contexts with destinations fail closed.
    Indented disclosure-tag lines outside fenced/raw blocks fail closed in multipart.
    Structural disclosure lines with comment-contained tag lookalikes also fail closed.
    Paragraphs with multiline inline dependencies remain indivisible; inline escapes,
    entities, spacing and potential emphasis dependencies stay intact at cuts.
    """
    details = 0
    details_starts: list[int | None] = []
    fence = ""
    fence_line = ""
    table_header = ""
    assessment_table = False
    previous = ""
    offset = previous_start = 0
    container_start = None
    container_blank = False
    inline_ambiguous = False
    destination_start = None
    destination_seen = False
    paragraph_start = indented_start = raw_start = None
    raw_end = None
    container_marker = re.compile(r">|[-+*](?=[ \t\r]|$)|[0-9]{1,9}[.)](?=[ \t\r]|$)")
    literal_marker = re.compile(r">+|[-+*#_]+|[0-9]{1,9}[.)](?=[ \t\r]|$)")
    disclosure_tag = re.compile(r"</?details(?=[ \t\f\r/>]|$)", re.IGNORECASE)
    summary_tag = re.compile(r"</?summary(?=[ \t\f\r/>]|$)", re.IGNORECASE)
    comment_end = re.compile(r"--!?>")
    reference_definition = re.compile(r"\[(?:\\[\s\S]|[^\[\]\\])+\]:")

    def protect_span(start: int, end: int, kind: str) -> None:
        if protected_spans is None:
            return
        protected_spans.append((start, end, kind))

    def emphasis_span(value: str, code_spans: list[tuple[int, int]]) -> tuple[int, int] | None:
        first = last = None
        code_index = 0
        for marker in re.finditer(r"[*_~]+", value):
            while code_index < len(code_spans) and code_spans[code_index][1] <= marker.start():
                code_index += 1
            if code_index < len(code_spans) and code_spans[code_index][0] <= marker.start():
                continue
            if (
                not marker[0].strip("_")
                and marker.start()
                and marker.end() < len(value)
                and value[marker.start() - 1].isalnum()
                and value[marker.end()].isalnum()
            ):
                continue
            slash_start = marker.start()
            while slash_start and value[slash_start - 1] == "\\":
                slash_start -= 1
            if (marker.start() - slash_start) % 2 and len(marker[0]) == 1:
                continue
            if first is None:
                first = marker.start()
            last = marker.end()
        return (first, last) if first is not None and last is not None else None

    def protect_paragraph(end: int) -> None:
        if paragraph_start is None:
            return
        paragraph = text[paragraph_start:end].removesuffix("\n")
        if "\n" not in paragraph:
            return
        code_spans = _disclosure_code_spans(paragraph) if "`" in paragraph else []
        unrecognized_start = paragraph.find("`", code_spans[-1][1] if code_spans else 0)
        emphasis = emphasis_span(paragraph, code_spans)
        if (
            any("\n" in paragraph[start:stop] for start, stop in code_spans)
            or (
                unrecognized_start >= 0
                and "\n" in paragraph[unrecognized_start : paragraph.rfind("`")]
            )
            or (emphasis is not None and "\n" in paragraph[emphasis[0] : emphasis[1]])
            or re.search(r"(?:\\| {2,})\r?\n", paragraph)
        ):
            protect_span(paragraph_start, end, "multiline paragraph")

    def visible_tags(
        pattern: re.Pattern[str],
        line: str,
        line_start: int,
        literal_spans: list[tuple[int, int]],
    ) -> list[re.Match[str]]:
        visible = []
        literal_index = 0
        for tag in pattern.finditer(line):
            while (
                literal_index < len(literal_spans)
                and literal_spans[literal_index][1] <= tag.start()
            ):
                literal_index += 1
            if literal_index < len(literal_spans):
                start, end = literal_spans[literal_index]
                if start <= tag.start() < end:
                    protect_span(line_start + start, line_start + end, "inline disclosure code")
                    continue
            visible.append(tag)
        return visible

    def protect_literals(line: str, line_start: int, active_fence: str = "") -> None:
        pattern = (
            rf" {{0,3}}({re.escape(active_fence[0])}{{{len(active_fence)},}})"
            if active_fence
            else r"(`{3,}|~{3,})"
        )
        for literal in re.finditer(pattern, line):
            start = max(0, literal.start() - 1)
            end = literal.end(1)
            if active_fence:
                if re.fullmatch(r" {0,3}", line[start : literal.start(1)]):
                    following = re.search(r"[^ \t]", line[end:])
                    end += following.end() if following else len(line) - end
            else:
                # Opening fences allow trailing info, so retain the left non-opening context.
                start = literal.start(1)
                while start and line[start - 1] in " \t":
                    start -= 1
                if start:
                    start -= 1
            protect_span(line_start + start, line_start + end, "literal fence run")

    for line in text.removesuffix("\n").split("\n"):
        line_start = offset
        offset += len(line) + 1
        if fence:
            destination_start, destination_seen = None, False
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*\r?", line):
                protect_span(line_start, min(offset, len(text)), "fence delimiter")
                fence = ""
                inline_ambiguous = False
            elif protected_spans is not None:
                protect_literals(line, line_start, fence)
            continue
        if raw_end is None:
            raw = re.match(
                r" {0,3}(<!--|<\?|<!\[CDATA\[|<![A-Z]|"
                r"<(pre|script|style|textarea)(?=[ \t\r>]))",
                line,
                re.IGNORECASE,
            )
            if raw:
                protect_paragraph(line_start)
                if raw[2] and line[raw.end() : raw.end() + 1] != ">":
                    if protected_spans is not None:
                        raise ValueError(
                            "Polly review attributed raw HTML is not supported "
                            "in multipart comments."
                        )
                raw_start = line_start
                raw_end = re.compile(
                    {"<!--": r"--!?>", "<?": r"\?>", "<![CDATA[": r"\]\]>"}.get(
                        raw[1].upper(), f"</{raw[2]}>" if raw[2] else ">"
                    ),
                    re.IGNORECASE,
                )
                paragraph_start = indented_start = None
                table_header = previous = ""
                assessment_table = inline_ambiguous = False
        if raw_end is not None:
            destination_start, destination_seen = None, False
            ending = raw_end.search(line)
            if ending:
                if ending[0] == "--!>" and protected_spans is not None:
                    raise ValueError(
                        "Polly review ambiguous HTML comments are not supported "
                        "in multipart comments."
                    )
                if protected_spans is not None and line[ending.end() :].strip(" \t\r"):
                    raise ValueError(
                        "Polly review raw HTML closing-line tails are not supported "
                        "in multipart comments."
                    )
                protect_span(raw_start, min(offset, len(text)), "raw HTML block")
                raw_start = raw_end = None
            continue
        if protected_spans is not None:
            position = 0
            container = False
            while True:
                while position < len(line) and line[position] in " \t":
                    position += 1
                marker = container_marker.match(line, position)
                if marker is None:
                    break
                container = True
                position = marker.end()
            if reference_definition.match(text, line_start + position):
                raise ValueError(
                    "Polly review reference definitions are not supported in multipart comments."
                )
            if container and line.startswith(("```", "~~~"), position):
                raise ValueError(
                    "Polly review container fences are not supported in multipart comments."
                )
            indentation = len(line) - len(line.lstrip(" \t"))
            blank = not line.strip(" \t\r")
            if (
                container_start is not None
                and container_blank
                and not blank
                and not indentation
                and not container
            ):
                protect_span(container_start, line_start, "Markdown container")
                container_start = None
            if container and container_start is None:
                container_start = line_start
            # Retain lazy/indented continuations until an unambiguous block boundary.
            container_blank = blank
        opening = re.match(
            r"^[ \t]*(`{3,}|~{3,})" if protected_spans is not None else r"^ {0,3}(`{3,}|~{3,})",
            line,
        )
        if opening and opening[1].startswith("`"):
            invalidating = line.find("`", opening.end())
            if invalidating >= 0:
                # Keep the backtick that makes the info string invalid with its opener.
                protect_span(line_start, line_start + invalidating + 1, "literal fence run")
                opening = None
        if opening:
            if protected_spans is not None and opening.start(1):
                raise ValueError(
                    "Polly review container fences are not supported in multipart comments."
                )
            fence, fence_line = opening[1], line
            destination_start, destination_seen = None, False
            inline_ambiguous = False
            protect_span(line_start, min(offset, len(text)), "fence delimiter")
            table_header = ""
            assessment_table = False
            protect_paragraph(line_start)
            paragraph_start = indented_start = None
            continue
        if protected_spans is not None:
            if not line.lstrip().startswith("|"):
                for marker in literal_marker.finditer(line):
                    start, end = marker.span()
                    if marker[0][0] != ">" and end < len(line) and line[end] not in " \t\r":
                        continue
                    while start and line[start - 1] in " \t":
                        start -= 1
                    start = max(0, start - 1)
                    while end < len(line) and line[end] in " \t":
                        end += 1
                    if line.startswith(("```", "~~~"), end):
                        raise ValueError(
                            "Polly review container fences are not supported "
                            "in multipart comments."
                        )
                    protect_span(
                        line_start + start,
                        line_start + min(end + 1, len(line)),
                        "literal container marker",
                    )
                for space in re.finditer(r"[ \t]+", line):
                    if space.start() and (len(space[0]) >= 4 or "\t" in space[0]):
                        protect_span(
                            line_start + space.start() - 1,
                            line_start + min(space.end() + 1, len(line)),
                            "literal indentation",
                        )
            protect_literals(line, line_start)
            if not line.strip(" \t\r") or (table_header and line.lstrip().startswith("|")):
                inline_ambiguous = False
            code_spans = (
                _disclosure_code_spans(line)
                if "`" in line
                and not re.match(r"\s*</?[A-Za-z][A-Za-z0-9-]*(?=[ \t\r/>]|$)", line)
                else []
            )
            literal_spans = [] if inline_ambiguous else code_spans
            cursor = 0
            for start, end in code_spans:
                inline_ambiguous |= "`" in line[cursor:start]
                cursor = end
            inline_ambiguous |= "`" in line[cursor:]

            if not line.strip(" \t\r") or line.lstrip().startswith("|"):
                destination_start, destination_seen = None, False
            bracket = line.find("[")
            if bracket >= 0 and destination_start is None:
                while bracket and line[bracket - 1] in "\\!":
                    bracket -= 1
                destination_start = line_start + bracket
            destination_seen |= "](" in line
            if destination_seen and destination_start is not None:
                closing = line.rfind(")")
                if closing >= 0:
                    protect_span(destination_start, line_start + closing + 1, "inline destination")
            for start, end in code_spans:
                protect_span(line_start + start, line_start + end, "inline code")
            unmatched = line.find("`", cursor)
            if unmatched >= 0:
                protect_span(
                    line_start + max(0, unmatched - 1),
                    line_start + min(len(line), line.rfind("`") + 2),
                    "ambiguous inline code",
                )
            for literal in re.finditer(
                r"\\[!-/:-@\[-`{-~]|&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);",
                line,
            ):
                protect_span(
                    line_start + literal.start(), line_start + literal.end(), "inline literal"
                )
            # Keep whitespace inside a fragment so edge trimming cannot remove it.
            for space in re.finditer(r"[ \t]+", line):
                if space.start() and space.end() < len(line):
                    if line.lstrip().startswith("|") and (
                        line[space.start() - 1] == "|" or line[space.end()] == "|"
                    ):
                        continue
                    protect_span(
                        line_start + space.start() - 1,
                        line_start + space.end() + 1,
                        "assessment spacing"
                        if assessment_table and line.lstrip().startswith("|")
                        else "inline spacing",
                    )
            for underscore in re.finditer(r"_+", line):
                if (
                    underscore.start()
                    and underscore.end() < len(line)
                    and line[underscore.start() - 1].isalnum()
                    and line[underscore.end()].isalnum()
                ):
                    protect_span(
                        line_start + underscore.start() - 1,
                        line_start + underscore.end() + 1,
                        "intraword underscore",
                    )
            emphasis = emphasis_span(line, code_spans)
            if emphasis is not None:
                protect_span(
                    line_start + max(0, emphasis[0] - 1),
                    line_start + min(len(line), emphasis[1] + 1),
                    "inline emphasis",
                )
            ambiguous_destination = inline_ambiguous and destination_seen
            for word in re.finditer(r"\S+", line):
                value = word[0]
                if (
                    "://" in value
                    or "www." in value
                    or "@" in value
                    or ("<" in value and ":" in value)
                ):
                    ambiguous_destination |= inline_ambiguous
                    protect_span(
                        line_start + word.start(), line_start + word.end(), "inline destination"
                    )

            tags = visible_tags(disclosure_tag, line, line_start, literal_spans)
            if tags and (
                not line.lstrip().startswith(("<details>", "</details>"))
                or any(
                    tag[0] not in {"<details", "</details"}
                    or line[tag.end() : tag.end() + 1] != ">"
                    for tag in tags
                )
            ):
                raise ValueError(
                    "Polly review noncanonical details are not supported in multipart comments."
                )
            summaries = visible_tags(summary_tag, line, line_start, literal_spans)
            if summaries and (
                [tag[0] for tag in summaries] != ["<summary", "</summary"]
                or not line.lstrip().startswith(("<details>", "<summary>"))
                or any(line[tag.end() : tag.end() + 1] != ">" for tag in summaries)
            ):
                raise ValueError(
                    "Polly review noncanonical summary is not supported in multipart comments."
                )
            if not validate_assessments:
                html_cursor = 0
                for html in visible_tags(
                    re.compile(r"<!--|</?[A-Za-z][A-Za-z0-9-]*(?=[ \t\r/>]|$)|<!|<\?"),
                    line,
                    line_start,
                    literal_spans,
                ):
                    if html.start() < html_cursor:
                        continue
                    if html[0] == "<!--":
                        ending = comment_end.search(line, html.end())
                        if ending is None or ending[0] == "--!>":
                            raise ValueError(
                                "Polly review ambiguous HTML comments are not supported "
                                "in multipart comments."
                            )
                        html_cursor = ending.end()
                        protect_span(line_start, min(offset, len(text)), "inline HTML comment")
                    elif html[0] not in {"<details", "</details", "<summary", "</summary"}:
                        raise ValueError(
                            "Polly review unsupported raw HTML is not supported "
                            "in multipart comments."
                        )
            cells = (
                split_table_row(line.strip().strip("|"))
                if "|" in line and re.fullmatch(r"[\s|:-]+", line)
                else []
            )
            delimiter = len(cells) > 1 and all(
                re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells
            )
            if (
                delimiter
                and "|" in previous
                and not (line.lstrip().startswith("|") and previous.lstrip().startswith("|"))
            ) or (table_header and "|" in line and not line.lstrip().startswith("|")):
                raise ValueError(
                    "Polly review tables without leading pipes are not supported "
                    "in multipart comments."
                )
            if ambiguous_destination:
                raise ValueError(
                    "Polly review ambiguous inline destinations are not supported "
                    "in multipart comments."
                )
        if re.match(r"(?: {4}| {0,3}\t)", line) and line.lstrip().startswith(
            ("<details>", "</details>", "<summary>")
        ):
            raise ValueError(
                "Polly review indented disclosure tags are not supported in multipart comments."
            )
        if line.lstrip().startswith(("<details>", "</details>", "<summary>")):
            comment_start = line.find("<!--")
            while comment_start >= 0:
                comment_close = comment_end.search(line, comment_start + 4)
                comment_stop = comment_close.start() if comment_close else len(line)
                if disclosure_tag.search(
                    line, comment_start + 4, comment_stop
                ) or summary_tag.search(line, comment_start + 4, comment_stop):
                    raise ValueError(
                        "Polly review commented disclosure tags are not supported "
                        "in multipart comments."
                    )
                if comment_close is None:
                    break
                comment_start = line.find("<!--", comment_close.end())
            for tag in re.findall(r"</?details>|</?summary>", line):
                if tag == "<details>":
                    details += 1
                    details_starts.append(line_start)
                elif tag == "</details>":
                    details = max(0, details - 1)
                    if details_starts:
                        details_starts.pop()
                elif tag == "</summary>":
                    start = details_starts[-1] if details_starts else None
                    protect_span(
                        line_start if start is None else start,
                        min(offset, len(text)),
                        "disclosure header",
                    )
                    if details_starts:
                        details_starts[-1] = None
        if line.lstrip().startswith("|"):
            cells = split_table_row(line.strip().strip("|"))
            if previous.lstrip().startswith("|") and all(
                re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells
            ):
                table_header = previous + "\n" + line + "\n"
                protect_span(previous_start, min(offset, len(text)), "table header")
                assessment_table = [
                    cell.strip().lower() for cell in split_table_row(previous.strip().strip("|"))
                ] == TEST_HEADER
            elif (
                validate_assessments
                and assessment_table
                and (len(cells) != TEST_FIELD_COUNT or not all(cell.strip() for cell in cells))
            ):
                raise ValueError(
                    "Polly review assessment fragments need all five populated fields."
                )
        else:
            table_header = ""
            assessment_table = False
        if protected_spans is not None:
            setext = paragraph_start is not None and re.fullmatch(
                r" {0,3}(?:-+|=+)[ \t]*\r?", line
            )
            indented = re.match(r"(?: {4}| {0,3}\t)", line)
            if setext:
                protect_span(paragraph_start, min(offset, len(text)), "setext heading")
            if indented:
                if indented_start is None:
                    indented_start = paragraph_start if paragraph_start is not None else line_start
                protect_span(indented_start, min(offset, len(text)), "indented block")
            elif line.strip(" \t\r"):
                indented_start = None
            if (
                setext
                or not line.strip(" \t\r")
                or table_header
                or line.lstrip().startswith(("<details>", "</details>", "<summary>"))
                or container_marker.match(line.lstrip(" \t"))
                or re.match(r" {0,3}#{1,6}(?:[ \t]|$)", line)
                or re.fullmatch(r" {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})\r?", line)
            ):
                protect_paragraph(line_start)
                paragraph_start = None
            elif not indented and paragraph_start is None:
                paragraph_start = line_start
        previous = line
        previous_start = line_start
    protect_paragraph(len(text))
    if container_start is not None:
        protect_span(container_start, len(text), "Markdown container")
    if raw_end is not None and protected_spans is not None:
        raise ValueError(
            "Polly review unclosed raw HTML blocks are not supported in multipart comments."
        )
    if protected_spans is not None:
        spans = sorted(protected_spans)
        protected_spans.clear()
        for start, end, kind in spans:
            if protected_spans and start < protected_spans[-1][1]:
                prior_start, prior_end, prior_kind = protected_spans.pop()
                start = prior_start
                literal_fence = "literal fence run" in {kind, prior_kind}
                if prior_end >= end:
                    end, kind = prior_end, prior_kind
                if literal_fence and kind in {
                    "inline spacing",
                    "inline emphasis",
                    "ambiguous inline code",
                }:
                    kind = "literal fence run"
            protected_spans.append((start, end, kind))
    prefix = "<details>\n<summary>Review continued</summary>\n\n" * details
    suffix = "\n\n</details>" * details
    if fence:
        prefix += fence_line + "\n"
        suffix = ("" if text.endswith("\n") else "\n") + fence + "\n" + suffix
        table_header = ""
    if continuing and table_header and not text.endswith("\n"):
        row = previous.strip()
        cells = split_table_row(row.strip("|"))
        if len(cells) != TEST_FIELD_COUNT:
            raise ValueError(
                "Polly review table row cannot continue within comment capacity "
                "without five parsed fields."
            )
        if row.endswith("|") and not row.endswith(r"\|"):
            return prefix, suffix, table_header
        prefix += table_header + "|" + "|".join(cells[:-1]) + "| "
        suffix = " |\n" + suffix
        table_header = ""
    return prefix, suffix, table_header


def review_comments(review: str, *, head_sha: str, run_id: str, run_url: str) -> list[str]:
    """Render all bounded comments before publishing any authoritative marker."""

    def wrap(content: str, part: int, count: int) -> str:
        marker = (
            f"<!-- polly-reviewed-sha: {head_sha} -->"
            if part == 1
            else f"<!-- polly-review-part:{run_id}:{part}/{count} -->"
        )
        title = (
            '## <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/'
            'docs/images/omnigent-logo.svg" alt="" height="20" valign="middle" /> Polly AI Review'
        )
        note = ""
        if count > 1:
            title += f" (part {part}/{count})"
            note = (
                f"Review of `{head_sha}`. Read all {count} parts. "
                "Continuations are posted before part 1; publication is complete only when "
                "part 1 appears.\n\n"
            )
        separator = "\n\n---\n" if count > 1 else "\n---\n"
        return (
            f"<!-- polly-review-bot -->\n{marker}\n<!-- polly-review-run:{run_id} -->\n"
            f"{title}\n\n{note}{content}{separator}"
            f"<sub>Automated review by Polly · [workflow run]({run_url})</sub>\n"
        )

    def validated(comments: list[str]) -> list[str]:
        for comment in comments:
            _markdown_context(
                comment,
                validate_assessments=True,
                protected_spans=[] if len(comments) > 1 else None,
            )
        return comments

    single = wrap(review, 1, 1)
    if len(single.encode("utf-8")) <= COMMENT_BYTE_LIMIT:
        return [single]
    if len(review.encode("utf-8")) > COMMENT_BYTE_LIMIT * MAX_REVIEW_PARTS:
        raise ValueError("Polly review exceeds the publication part limit.")
    work_left = RENDER_WORK_BYTE_LIMIT

    def charge_work(*texts: str) -> None:
        nonlocal work_left
        work_left -= sum(len(text.encode("utf-8")) for text in texts)
        if work_left < 0:
            raise ValueError("Polly review exceeds the rendering work budget.")

    charge_work(review)
    protected_spans: list[tuple[int, int, str]] = []
    _markdown_context(review, continuing=False, protected_spans=protected_spans)
    contents = []
    before = ""
    pending = review
    context = ("", "", "")
    while pending:
        if len(contents) >= MAX_REVIEW_PARTS:
            raise ValueError("Polly review exceeds the publication part limit.")
        capacity = COMMENT_BYTE_LIMIT
        while True:
            charge_work(pending, before)
            prefix, _, table_header = context
            chunk = split_review(
                pending, capacity, in_table_row=prefix.endswith("| "), split_once=True
            )[0]
            end = len(before) + len(chunk)
            for span_start, span_end, kind in protected_spans:
                if span_start >= end:
                    break
                if span_start < end < span_end:
                    if span_start <= len(before):
                        if kind == "assessment spacing":
                            raise ValueError(
                                "Polly review assessment fragments need all five populated fields."
                            )
                        raise ValueError(
                            f"Polly review {kind} cannot fit within comment capacity."
                        )
                    chunk = chunk[: span_start - len(before)]
                    break
            tail = pending[len(chunk) :]
            next_before = before + chunk
            charge_work(next_before)
            next_context = _markdown_context(next_before, continuing=bool(tail))
            _, suffix, _ = next_context
            if chunk.lstrip(" \t").startswith("|"):
                prefix += table_header
            content = prefix + chunk + suffix
            charge_work(content)
            # Reserve the largest count so accepted parts never need rebuilding.
            comment = wrap(content, len(contents) + 1, MAX_REVIEW_PARTS)
            overflow = len(comment.encode("utf-8")) - COMMENT_BYTE_LIMIT
            if overflow > 0:
                capacity = len(chunk.encode("utf-8")) - overflow
                continue
            contents.append(content)
            before, pending, context = next_before, tail, next_context
            break
    comments = [wrap(content, part, len(contents)) for part, content in enumerate(contents, 1)]
    charge_work(*comments)
    return validated(comments)


def publish_review(review: Path, receipt: Path) -> None:
    receipt.unlink(missing_ok=True)
    comments = review_comments(
        review.read_text(encoding="utf-8"),
        head_sha=os.environ["HEAD_SHA"],
        run_id=f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
        run_url=os.environ["RUN_URL"],
    )
    paths = []
    for part, comment in enumerate(comments, 1):
        path = review.with_name("comment.md" if part == 1 else f"comment-part-{part}.md")
        path.write_text(comment, encoding="utf-8")
        paths.append(path)
    # Continuations cannot suppress a retry. Publish the reviewed-SHA marker last.
    for path in [*paths[1:], paths[0]]:
        subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                os.environ["PR_NUMBER"],
                "--repo",
                os.environ["REPO"],
                "--body-file",
                str(path),
            ],
            check=True,
        )
    receipt.write_text(os.environ["HEAD_SHA"] + "\n", encoding="utf-8")


def linked_issues(pull_request: str) -> dict | None:
    """Return the issues a PR closes, or None when GitHub cannot be read."""
    try:
        repository, number = pull_request.rsplit("#", 1)
        owner, name = repository.split("/", 1)
        response = subprocess.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
                "-f",
                f"query={LINKED_ISSUES_QUERY}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        references = json.loads(response.stdout)["data"]["repository"]["pullRequest"][
            "closingIssuesReferences"
        ]
        issues = []
        for node in filter(None, references["nodes"]):
            body = node.get("body") or ""
            issues.append(
                {
                    "issue": f"{node['repository']['nameWithOwner']}#{node['number']}",
                    "title": node["title"],
                    "state": node["state"],
                    "body": body[:LINKED_ISSUE_BODY_LIMIT],
                    "body_truncated": len(body) > LINKED_ISSUE_BODY_LIMIT,
                }
            )
        omitted = max(references["totalCount"] - len(issues), 0)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        detail = getattr(error, "stderr", None) or error
        print(
            "::warning::Could not read linked issues; scope is reviewed from the PR description. "
            f"({type(error).__name__}: {' '.join(str(detail).split())[:500]})"
        )
        return None
    return {"issues": issues, "omitted": omitted}


def main() -> None:
    mode, source, destination = sys.argv[1:]
    if mode == "stats":
        Path(destination).write_text(json.dumps(diff_stats(Path(source))), encoding="utf-8")
    elif mode == "linked-issues":
        Path(destination).write_text(json.dumps(linked_issues(source)), encoding="utf-8")
    elif mode == "compose":
        stats = json.loads(Path(source).read_text(encoding="utf-8"))
        review = Path(destination)
        result = compose_review(review.read_text(encoding="utf-8"), stats)
        review.write_text(result, encoding="utf-8")
    elif mode == "publish":
        try:
            publish_review(Path(source), Path(destination))
        except subprocess.CalledProcessError as error:
            raise SystemExit(error.returncode) from error
    else:
        raise ValueError(f"Unknown command: {mode}")


if __name__ == "__main__":
    main()
