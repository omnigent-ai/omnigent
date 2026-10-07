"""Compute diff metrics and enforce the published Polly summary structure."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

TESTS = "Tests and test support"
CATEGORIES = (TESTS, "Documentation", "Dependencies and lockfiles", "Implementation / other")


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


def compose_review(review: str, stats: dict) -> str:
    summaries = list(re.finditer(r"^## Summary\s*$", review, re.MULTILINE))
    if len(summaries) != 1:
        raise ValueError("Incomplete Polly review: expected exactly one '## Summary'.")
    summary = summaries[0]
    body = re.split(r"^## ", review[summary.end() :], maxsplit=1, flags=re.MULTILINE)[0]
    sections = {}
    for name in ("Changes", "Tests", "Scope"):
        matches = list(re.finditer(rf"^### {name}\s*$", body, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"Incomplete Polly review: expected one '### {name}' in Summary.")
        text = re.split(r"^### ", body[matches[0].end() :], maxsplit=1, flags=re.MULTILINE)[0]
        if not text.strip():
            raise ValueError(f"Incomplete Polly review: empty '{name}' assessment.")
        sections[name] = text
    rows = []
    if stats["test_files"]:
        header = ["test / case", "behavior protected", "layer", "needed?", "action / rationale"]
        header_seen = False
        for line in sections["Tests"].splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if [cell.lower() for cell in cells] == header:
                header_seen = True
                continue
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if len(cells) != 5 or not all(cells):
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
    # A slash inside a parameterized case name is not a file path.
    case_only = all(
        (prefix := row.lstrip("`* ").split("[", 1)[0]) and "/" not in prefix for row in rows
    )
    if (
        len(stats["test_files"]) == 1
        and rows
        and case_only
        and missing
        and missing[0] in sections["Tests"]
    ):
        missing = []
    if missing:
        raise ValueError(f"Incomplete Polly review: unassessed test files: {', '.join(missing)}")
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


def main() -> None:
    mode, source, destination = sys.argv[1:]
    if mode == "stats":
        Path(destination).write_text(json.dumps(diff_stats(Path(source))), encoding="utf-8")
    elif mode == "compose":
        stats = json.loads(Path(source).read_text(encoding="utf-8"))
        review = Path(destination)
        result = compose_review(review.read_text(encoding="utf-8"), stats)
        review.write_text(result, encoding="utf-8")
    else:
        raise ValueError(f"Unknown command: {mode}")


if __name__ == "__main__":
    main()
