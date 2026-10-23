"""Compute diff metrics and enforce the published Polly summary structure."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

TESTS = "Tests and test support"
CATEGORIES = (TESTS, "Documentation", "Dependencies and lockfiles", "Implementation / other")
LINKED_ISSUE_BODY_LIMIT = 4096
LINKED_ISSUES_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    closingIssuesReferences(first: 5) {
      totalCount nodes { number title state body repository { nameWithOwner } }
    }
  } }
}"""
# Matches uv.toml's exclude-newer and the release hold on internal PyPI mirrors.
DEPENDENCY_COOLDOWN_DAYS = 7
DEPENDENCY_LOOKUP_LIMIT = 40
REQUIREMENT_LINE = re.compile(r'^([+-])\s*"([^"]+)",?\s*(?:#.*)?$')


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


def changed_requirements(diff_text: str) -> list[dict]:
    """Return requirements a diff adds to pyproject.toml files, with the line each replaces."""
    # Imported lazily: the other modes run on the runner's bare python3.
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

    current_file = None
    removed: dict[tuple[str, str], str] = {}
    added: list[tuple[tuple[str, str], str]] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/\S+ b/(\S+)$", line)
            is_manifest = match and PurePosixPath(match.group(1)).name == "pyproject.toml"
            current_file = match.group(1) if is_manifest else None
            continue
        match = REQUIREMENT_LINE.match(line)
        if current_file is None or line.startswith(("+++", "---")) or not match:
            continue
        try:
            requirement = Requirement(match.group(2))
        except InvalidRequirement:
            continue
        key = (current_file, canonicalize_name(requirement.name))
        if match.group(1) == "-":
            removed[key] = match.group(2)
        elif requirement.specifier:
            added.append((key, match.group(2)))
    return [
        {"file": file, "name": name, "requirement": text, "previous": removed.get((file, name))}
        for (file, name), text in added
        if removed.get((file, name)) != text
    ]


def pypi_release_uploads(name: str) -> dict[str, datetime]:
    """Return each PyPI release's first non-yanked upload time."""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
    with urllib.request.urlopen(url, timeout=10) as response:
        releases = json.load(response)["releases"]
    uploads = {}
    for version, files in releases.items():
        times = [
            datetime.fromisoformat(file["upload_time_iso_8601"].replace("Z", "+00:00"))
            for file in files
            if not file.get("yanked")
        ]
        if times:
            uploads[version] = min(times)
    return uploads


def repository_package_names(root: Path) -> set[str]:
    """Return the distribution names published from this repository."""
    from packaging.utils import canonicalize_name

    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*pyproject.toml"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    names = set()
    for path in filter(None, listed.decode().split("\0")):
        name = (
            tomllib.loads((root / path).read_text(encoding="utf-8")).get("project", {}).get("name")
        )
        if name:
            names.add(canonicalize_name(name))
    return names


def young_dependency_requirements(
    diff_text: str,
    repository_packages: set[str],
    fetch_uploads: Callable[[str], dict[str, datetime]] = pypi_release_uploads,
    now: datetime | None = None,
) -> list[dict]:
    """Return changed requirements that no release older than the cooldown satisfies."""
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion, Version

    cutoff = (now or datetime.now(UTC)) - timedelta(days=DEPENDENCY_COOLDOWN_DAYS)
    changes = [
        change
        for change in changed_requirements(diff_text)
        if change["name"] not in repository_packages
    ]
    findings = []
    for change in changes[:DEPENDENCY_LOOKUP_LIMIT]:
        try:
            uploads = fetch_uploads(change["name"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            findings.append({**change, "lookup_error": type(error).__name__})
            continue
        releases = {}
        for version, uploaded in uploads.items():
            try:
                releases[Version(version)] = uploaded
            except InvalidVersion:
                continue
        specifier = Requirement(change["requirement"]).specifier
        if any(specifier.filter(v for v, uploaded in releases.items() if uploaded <= cutoff)):
            continue
        satisfying = list(specifier.filter(releases))
        oldest = min(satisfying, key=releases.__getitem__) if satisfying else None
        findings.append(
            {
                **change,
                "oldest_satisfying_release": str(oldest) if oldest else None,
                "uploaded_at": releases[oldest].isoformat() if oldest else None,
            }
        )
    return findings


def main() -> None:
    mode, source, destination = sys.argv[1:]
    if mode == "dependency-age":
        findings = young_dependency_requirements(
            Path(source).read_text(encoding="utf-8", errors="replace"),
            repository_package_names(Path.cwd()),
        )
        result = {"cooldown_days": DEPENDENCY_COOLDOWN_DAYS, "requirements": findings}
        Path(destination).write_text(json.dumps(result), encoding="utf-8")
    elif mode == "stats":
        Path(destination).write_text(json.dumps(diff_stats(Path(source))), encoding="utf-8")
    elif mode == "linked-issues":
        Path(destination).write_text(json.dumps(linked_issues(source)), encoding="utf-8")
    elif mode == "compose":
        stats = json.loads(Path(source).read_text(encoding="utf-8"))
        review = Path(destination)
        result = compose_review(review.read_text(encoding="utf-8"), stats)
        review.write_text(result, encoding="utf-8")
    else:
        raise ValueError(f"Unknown command: {mode}")


if __name__ == "__main__":
    main()
