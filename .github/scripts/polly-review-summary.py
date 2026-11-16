"""Compute diff metrics and enforce the published Polly summary structure."""

from __future__ import annotations

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


# How the Codex executor logs a tool result, after the log redactor has run over it:
# "Codex dynamic tool result: ... tool=sys_read_inbox status=success result={...}".
_TOOL_RESULT_LOG = "Codex dynamic tool result: "
_INBOX_READ_LOG = "tool=sys_read_inbox status=success result="
_INBOX_RESULT_PREFIX = '{"result": "'
_EMPTY_INBOX = "Inbox is empty"
_JSON_ESCAPE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})')
_JSON_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_INBOX_TRUNCATION = re.compile(
    r"\n\.\.\.\[truncated (\d+) chars — read the full text with sys_session_get_history[^\]\n]*\]"
)
_PARTIAL_BUDGET = 58000
# A section's markup, summary and cut note, around its fenced text.
_SECTION_OVERHEAD = 400
_PARTIAL_INTRO = (
    "Polly ran out of time before writing its review. Below is what its reviewers had "
    "reported, as Polly's inbox delivered it, unedited. Polly didn't check these reports "
    "against each other or the code, so treat what they flag as unverified. The commit "
    "still counts as unreviewed, so `/review` runs again.\n"
)


def _inbox_text(raw: str) -> str | None:
    """The text of one logged ``sys_read_inbox`` result, or ``None``."""
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except ValueError:
        # Redacting a secret can consume the result's closing quote and brace, so decode
        # what is left of the string instead of losing every report it carries.
        if not raw.startswith(_INBOX_RESULT_PREFIX):
            return None
        body = raw[len(_INBOX_RESULT_PREFIX) :].removesuffix('"}')
        value = {
            "result": _JSON_ESCAPE.sub(
                lambda match: (
                    chr(int(match[1][1:], 16)) if match[1][0] == "u" else _JSON_ESCAPES[match[1]]
                ),
                body,
            )
        }
    text = value.get("result") if isinstance(value, dict) else value
    if not isinstance(text, str):
        return None
    # Pair escaped surrogates, and replace any left alone, so the text encodes.
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace").strip()


def inbox_reads(logs: Path) -> tuple[list[str], bool]:
    """Return each successful inbox read Polly logged, in order, and if any were Codex logs.

    A read stays whole: reports can quote the inbox's own framing, so splitting it into
    items could drop findings or attribute them to the wrong sub-agent.
    """
    reads: list[str] = []
    codex_logs = False
    for log in sorted(logs.rglob("*.log")):
        # Records end at LF; results keep other line breaks, such as U+2028, as written.
        for line in log.read_text(encoding="utf-8", errors="replace").split("\n"):
            codex_logs = codex_logs or _TOOL_RESULT_LOG in line
            position = line.find(_INBOX_READ_LOG)
            if position < 0:
                continue
            text = _inbox_text(line[position + len(_INBOX_READ_LOG) :].rstrip("\r"))
            if text and not text.startswith(_EMPTY_INBOX) and text not in reads:
                reads.append(text)
    return reads, codex_logs


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run in *text*."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = re.sub(re.escape(secret), "[REDACTED]", text, flags=re.IGNORECASE)
    return text


def partial_review(reads: list[str], secrets: list[str]) -> str:
    """Markdown listing each inbox read for a run that ran out of time, within one comment.

    Reads are posted unedited, so each is fenced: mentions don't notify, and markup
    can't render. ``<!--`` is escaped so quoted markers can't mark the commit reviewed.
    """
    used = len(_PARTIAL_INTRO.encode("utf-8"))
    count = max(len(reads), 1)
    share = max((_PARTIAL_BUDGET - used - _SECTION_OVERHEAD * count) // count, 2000)
    sections = [_PARTIAL_INTRO]
    for index, read in enumerate(reads):
        read = _INBOX_TRUNCATION.sub(
            r"\n…[\1 more characters are in this run's diagnostics artifact]",
            _redact(read, secrets),
        ).replace("<!--", "&lt;!--")
        # Budget the escaped text, leaving room for a fence longer than three backticks.
        limit = max(share - 2 * len(_fence(read)), 1000)
        encoded = read.encode("utf-8")
        if len(encoded) > limit:
            kept = encoded[:limit].decode("utf-8", errors="ignore").rstrip()
            read = (
                f"{kept}\n…[cut to fit one comment; the rest is in this run's diagnostics "
                "artifact]"
            )
        fence = _fence(read)
        section = (
            f"<details>\n<summary>Reports Polly received, inbox read {index + 1} of "
            f"{len(reads)}</summary>\n\n{fence}text\n{read}\n{fence}\n\n</details>\n"
        )
        if used + len(section.encode("utf-8")) > _PARTIAL_BUDGET:
            sections.append(
                f"{len(reads) - index} more inbox reads are in this run's diagnostics artifact.\n"
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
        # Writes nothing when no report can be recovered, so the caller posts nothing.
        reads, codex_logs = inbox_reads(Path(source))
        if not reads:
            reason = (
                "no reviewer had reported before the time limit"
                if codex_logs
                else "only the Codex harness's logs record what Polly's inbox delivered"
            )
            print(f"::notice::No reviewer reports to post: {reason}.")
            return
        key, gateway = (os.environ.get(name, "") for name in ("LLM_API_KEY", "GATEWAY_BASE_URL"))
        # The key, and the gateway as a URL, without its path, and as a bare host.
        address = urlsplit(gateway)
        values = {key, gateway, gateway.rstrip("/").removesuffix("/serving-endpoints")}
        values |= {address.netloc, address.hostname or ""}
        secrets = sorted(filter(None, values), key=len, reverse=True)
        Path(destination).write_text(partial_review(reads, secrets), encoding="utf-8")
    else:
        raise ValueError(f"Unknown command: {mode}")


if __name__ == "__main__":
    main()
