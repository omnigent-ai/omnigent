"""Compute diff metrics and enforce the published Polly summary structure."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

TESTS = "Tests and test support"
CATEGORIES = (TESTS, "Documentation", "Dependencies and lockfiles", "Implementation / other")
TEST_FIELD_COUNT = 5
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
    result = (
        review[: summary.end()].rstrip()
        + "\n\n"
        + stats["markdown"]
        + "\n\n"
        + collapse_test_assessment(review[summary.end() :].lstrip())
    )
    if len(result.encode("utf-8")) > 60000:
        raise ValueError(
            "Polly review exceeds the comment budget; refusing to truncate assessments."
        )
    return result


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


# A sub-agent's result as the runner delivers it to Polly's inbox, e.g.
# "[System: sub-agent task 0f1e completed — codex:review-api returned: ...]".
_SUBAGENT_RESULT = re.compile(
    r"\[System: sub-agent task [0-9a-f]+ (?:"
    r"completed — (?P<done>[^\n]+?) returned: "
    r"|cancelled — (?P<stopped>[^\n]+?); output before cancellation: )"
)
# The inbox joins its items with a blank line, and each item ends with "]".
_INBOX_ITEM_BREAK = re.compile(
    r"\]\n\n(?=\[System: (?:sub-agent task|task|inbox item|malformed) )"
)
# How the Codex executor logs a successful inbox read.
_INBOX_READ_LOG = "tool=sys_read_inbox status=success result="
_INBOX_TRUNCATION = re.compile(r"\n\.\.\.\[truncated (\d+) chars[^\]\n]*\]$")
_PARTIAL_BUDGET = 58000
# A section's markup, summary and cut note, around its fenced report.
_SECTION_OVERHEAD = 400
_PARTIAL_INTRO = (
    "Polly ran out of time before writing its review. Its reviewers finished the reports "
    "below, but Polly didn't check them against each other or the code, so treat what they "
    "flag as unverified. The commit still counts as unreviewed, so `/review` runs again.\n"
)


def reviewer_reports(logs: Path) -> list[tuple[str, str]]:
    """Return ``(sub-agent, report)`` pairs Polly read from its inbox, in order.

    Only successful ``sys_read_inbox`` results count, so text another tool returned can't
    pass for a report. One read can carry several items.
    """
    reports: list[tuple[str, str]] = []
    for log in sorted(logs.rglob("*.log")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            position = line.find(_INBOX_READ_LOG)
            if position < 0:
                continue
            try:
                result, _ = json.JSONDecoder().raw_decode(line, position + len(_INBOX_READ_LOG))
            except ValueError:
                continue
            text = result.get("result") if isinstance(result, dict) else result
            if not isinstance(text, str):
                continue
            parts = _INBOX_ITEM_BREAK.split(text)
            # The split takes each item's closing "]" but the last one's.
            for item in [part + "]" for part in parts[:-1]] + parts[-1:]:
                match = _SUBAGENT_RESULT.match(item)
                if match is None:
                    continue
                body = item[match.end() :].rstrip().removesuffix("]")
                body = body.removesuffix(", superseding its earlier cancelled notice").strip()
                report = (match["done"] or match["stopped"], body)
                if body and report not in reports:
                    reports.append(report)
    return reports


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run in *text*."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = re.sub(re.escape(secret), "[REDACTED]", text, flags=re.IGNORECASE)
    return text


def partial_review(reports: list[tuple[str, str]], secrets: list[str]) -> str:
    """Markdown listing *reports* for a run that ran out of time, within one comment.

    Reports are posted unedited, so each is fenced: mentions don't notify, and markup
    can't render. ``<!--`` is escaped so quoted markers can't mark the commit reviewed.
    """
    used = len(_PARTIAL_INTRO.encode("utf-8"))
    count = max(len(reports), 1)
    share = max((_PARTIAL_BUDGET - used - _SECTION_OVERHEAD * count) // count, 2000)
    sections = [_PARTIAL_INTRO]
    for index, (target, report) in enumerate(reports):
        report = _INBOX_TRUNCATION.sub(
            r"\n…[\1 more characters are in this run's diagnostics artifact]",
            _redact(report, secrets),
        ).replace("<!--", "&lt;!--")
        # Budget the escaped text, leaving room for a fence longer than three backticks.
        limit = max(share - 2 * len(_fence(report)), 1000)
        encoded = report.encode("utf-8")
        if len(encoded) > limit:
            kept = encoded[:limit].decode("utf-8", errors="ignore").rstrip()
            report = (
                f"{kept}\n…[cut to fit one comment; the rest is in this run's diagnostics "
                "artifact]"
            )
        fence = _fence(report)
        summary = html.escape(_redact(target, secrets)[:120])
        section = (
            f"<details>\n<summary>Report from {summary}</summary>\n\n"
            f"{fence}text\n{report}\n{fence}\n\n</details>\n"
        )
        if used + len(section.encode("utf-8")) > _PARTIAL_BUDGET:
            sections.append(
                f"{len(reports) - index} more reports are in this run's diagnostics artifact.\n"
            )
            break
        sections.append(section)
        used += len(section.encode("utf-8"))
    return "\n".join(sections)


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
    elif mode == "partial":
        # Writes nothing when no reviewer finished, so the caller posts nothing.
        reports = reviewer_reports(Path(source))
        if reports:
            key, gateway = (
                os.environ.get(name, "") for name in ("LLM_API_KEY", "GATEWAY_BASE_URL")
            )
            # The key, and the gateway as a URL, without its path, and as a bare host.
            address = urlsplit(gateway)
            values = {key, gateway, gateway.rstrip("/").removesuffix("/serving-endpoints")}
            values |= {address.netloc, address.hostname or ""}
            secrets = sorted(filter(None, values), key=len, reverse=True)
            Path(destination).write_text(partial_review(reports, secrets), encoding="utf-8")
    else:
        raise ValueError(f"Unknown command: {mode}")


if __name__ == "__main__":
    main()
