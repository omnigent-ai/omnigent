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
COMMENT_BYTE_LIMIT = 60000
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
        header = ["test / case", "behavior protected", "layer", "needed?", "action / rationale"]
        header_seen = False
        for line in sections["Tests"].splitlines():
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


def split_review(review: str, byte_limit: int) -> list[str]:
    """Partition without dropping text, preferring complete Markdown lines."""
    if byte_limit < 4:
        raise ValueError("Comment wrappers leave no room for review content.")
    parts = []
    in_table_row = False
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
            if in_table_row and re.fullmatch(r"\s*\|?\s*", review[end:].split("\n", 1)[0]):
                # Keep nonblank rationale text with the terminal delimiter and whitespace.
                content = prefix.rstrip()
                if content.endswith("|") and not content.endswith(r"\|"):
                    content = content[:-1].rstrip()
                end = len(content) - 1
                if end <= 0:
                    raise ValueError("Polly review assessment tail exceeds comment capacity.")
            if review[end - 1 : end + 1] == r"\|":
                end -= 1
        parts.append(review[:end])
        review = review[end:]
    parts.append(review)
    return parts


def _markdown_context(text: str) -> tuple[str, str, str]:
    """Reopen details, fenced code and table context at a comment boundary."""
    details = 0
    fence = ""
    fence_line = ""
    table_header = ""
    previous = ""
    for line in text.splitlines():
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = ""
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence, fence_line = opening[1], line
            table_header = ""
            continue
        if line.lstrip().startswith(("<details>", "</details>")):
            for tag in re.findall(r"</?details>", line):
                details = max(0, details + (1 if tag == "<details>" else -1))
        if line.lstrip().startswith("|"):
            cells = split_table_row(line.strip().strip("|"))
            if previous.lstrip().startswith("|") and all(
                re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells
            ):
                table_header = previous + "\n" + line + "\n"
        else:
            table_header = ""
        previous = line
    prefix = "<details>\n<summary>Review continued</summary>\n\n" * details
    suffix = "\n\n</details>" * details
    if fence:
        prefix += fence_line + "\n"
        suffix = "\n" + fence + "\n" + suffix
        table_header = ""
    if table_header and not text.endswith("\n"):
        row = previous.strip()
        cells = split_table_row(row.strip("|"))
        if len(cells) != TEST_FIELD_COUNT:
            raise ValueError("Polly review assessment fields exceed comment capacity.")
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

    single = wrap(review, 1, 1)
    if len(single.encode("utf-8")) <= COMMENT_BYTE_LIMIT:
        return [single]
    byte_limit = COMMENT_BYTE_LIMIT
    while True:
        chunks = split_review(review, byte_limit)
        comments = []
        before = ""
        context = ("", "", "")
        for part, chunk in enumerate(chunks, 1):
            prefix, _, table_header = context
            before += chunk
            context = _markdown_context(before)
            _, suffix, _ = context
            if chunk.lstrip().startswith("|"):
                prefix += table_header
            comments.append(wrap(prefix + chunk + suffix, part, len(chunks)))
        overflow = max(len(comment.encode("utf-8")) for comment in comments) - COMMENT_BYTE_LIMIT
        if overflow <= 0:
            return comments
        byte_limit -= overflow


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
