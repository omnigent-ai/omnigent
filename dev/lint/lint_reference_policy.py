"""Check added text and paths against the repository reference policy.

Inside EXISTING_REFERENCE_SCOPE, added text and paths may reuse a reference that the
scope already contains at the base revision; new references are still reported.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path

POLICY_FILES = frozenset({"dev/lint/lint_reference_policy.py"})
# Desktop shell paths; entries ending in "/" are directories.
EXISTING_REFERENCE_SCOPE = ("web/electron/", "web/src/lib/nativeBridge.ts")
PROHIBITED_NAMES = frozenset(
    {
        "arca",
        "arcas",
        "arclet",
        "arclets",
        "isaac",
        "dbcert",
        "dbcerts",
        "lakebox",
        "ucode",
        "dbexec",
        "spog",
    }
)
PHRASES = re.compile(
    r"databricks[\s_-]*(?:internal|corp(?:orate)?|employees?|network|only)"
    r"|(?:internal|corp(?:orate)?)[\s_-]*databricks"
    r"|databricks-eng(?:/|\b)"
    r"|\binternal[-_]beta\b"
    r"|\be2[-_]dogfood\b",
    re.IGNORECASE,
)
HOST_SUFFIXES = ("databricks.com", "databricksapps.com", "azuredatabricks.net")
PUBLIC_HOSTS = frozenset(
    {
        "www.databricks.com",
        "docs.databricks.com",
        "accounts.cloud.databricks.com",
        "login.databricks.com",
    }
)


def host_pattern(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(suffix) for suffix in suffixes)
    return re.compile(
        r"(?<![\w.-])(?:[a-z0-9-]+\.)+(?:" + alternatives + r")(?![\w-]|\.[\w-])",
        re.IGNORECASE,
    )


HOST = host_pattern(HOST_SUFFIXES)


def _line_references(text: str) -> dict[int, set[tuple[str, str]]]:
    """Map LF-based line numbers, including wrapped phrases, to normalized references."""
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", words)
    references: dict[int, set[tuple[str, str]]] = {}
    original_lines = text.split("\n")
    for line_number, normalized in enumerate(words.split("\n"), 1):
        line_references = {
            ("name", name)
            for name in PROHIBITED_NAMES.intersection(re.findall(r"[a-z]+", normalized.lower()))
        }
        for match in HOST.finditer(original_lines[line_number - 1]):
            host = match.group().lower()
            if host not in PUBLIC_HOSTS:
                line_references.add(("host", host))
        if line_references:
            references[line_number] = line_references
    line_starts = [0, *(match.end() for match in re.finditer("\n", words))]
    position = 0
    while match := PHRASES.search(words, position):
        # PHRASES treats case and space, underscore, and hyphen separators alike.
        phrase = ("phrase", re.sub(r"[\s_-]+", "", match.group().lower()))
        first_line = bisect_right(line_starts, match.start())
        for offset, segment in enumerate(match.group().split("\n")):
            if segment.strip():
                references.setdefault(first_line + offset, set()).add(phrase)
        position = match.start() + 1
    return references


def violates(text: str) -> bool:
    """Match names in prose, snake_case, camelCase, and filenames."""
    return bool(_line_references(text))


def in_existing_reference_scope(path: str) -> bool:
    return any(
        path.startswith(entry) if entry.endswith("/") else path == entry
        for entry in EXISTING_REFERENCE_SCOPE
    )


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def existing_scope_references(root: Path, revision: str) -> set[tuple[str, str]]:
    """Return references in scope paths and text blobs at a revision."""
    entries = git(root, "ls-tree", "-r", "-z", revision, "--", *EXISTING_REFERENCE_SCOPE)
    references: set[tuple[str, str]] = set()
    object_ids = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        for line_references in _line_references(os.fsdecode(path)).values():
            references |= line_references
        _mode, object_type, object_id = metadata.split()
        if object_type == b"blob":
            object_ids.append(object_id)
    if not object_ids:
        return references
    output = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        check=True,
        capture_output=True,
        input=b"".join(object_id + b"\n" for object_id in object_ids),
    ).stdout
    position = 0
    while position < len(output):
        header_end = output.index(b"\n", position)
        size = int(output[position:header_end].split()[2])
        contents = output[header_end + 1 : header_end + 1 + size]
        position = header_end + 2 + size
        if b"\0" in contents[:8000]:
            continue
        for line_references in _line_references(
            contents.decode("utf-8", errors="replace")
        ).values():
            references |= line_references
    return references


def check(root: Path, base: str | None = None, head: str | None = None) -> list[str]:
    """Check staged changes, or the branch diff from a merge base."""
    if head and not base:
        raise ValueError("--head requires --base")
    revisions = ["--cached"]
    base_revision = "HEAD"
    if base:
        target = head or "HEAD"
        base_revision = git(root, "merge-base", base, target).decode().strip()
        revisions = [base_revision, target]
    options = ["--no-ext-diff", "--no-textconv", "--no-color"]
    entries = iter(
        git(
            root,
            "diff",
            *options,
            "--find-renames",
            *revisions,
            "--raw",
            "--no-abbrev",
            "-z",
            "--diff-filter=AMRT",
            "--",
        ).split(b"\0")
    )
    findings = []
    scope_references: set[tuple[str, str]] | None = None

    def new_to_scope(references: set[tuple[str, str]]) -> bool:
        nonlocal scope_references
        if scope_references is None:
            scope_references = existing_scope_references(root, base_revision)
        return not references <= scope_references

    for metadata in entries:
        if not metadata:
            break
        old_mode, mode, old_object_id, object_id, status = metadata[1:].split()
        old_path = os.fsdecode(next(entries))
        renamed = status.startswith(b"R")
        path = os.fsdecode(next(entries)) if renamed else old_path
        if path in POLICY_FILES:
            continue
        in_scope = in_existing_reference_scope(path)
        scope_note = " (new to the existing-reference scope)" if in_scope else ""
        if status == b"A" or renamed:
            path_references = set().union(*_line_references(path).values())
            if path_references and (not in_scope or new_to_scope(path_references)):
                findings.append(f"{path}:1: prohibited reference in new path{scope_note}")
        if mode == b"160000":
            continue
        contents = git(root, "cat-file", "blob", object_id.decode())
        if b"\0" in contents[:8000]:
            continue
        line_references = _line_references(contents.decode("utf-8", errors="replace"))
        if not line_references:
            continue
        violation_lines = set(line_references)
        if status == b"A" or old_mode == b"160000" or old_path in POLICY_FILES:
            added_lines = violation_lines
        else:
            patch = git(
                root,
                "diff",
                *options,
                "--text",
                "--unified=0",
                "--inter-hunk-context=0",
                old_object_id.decode(),
                object_id.decode(),
                "--",
            ).decode("utf-8", errors="replace")
            added_lines = set()
            line_number = None
            for line in patch.split("\n"):
                hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if hunk:
                    line_number = int(hunk[1])
                elif line_number is not None and line.startswith("+"):
                    added_lines.add(line_number)
                    line_number += 1
                elif line_number is not None and line.startswith(" "):
                    line_number += 1
        findings.extend(
            f"{path}:{line_number}: prohibited reference in added text{scope_note}"
            for line_number in sorted(violation_lines & added_lines)
            if not in_scope or new_to_scope(line_references[line_number])
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("PRE_COMMIT_FROM_REF"))
    parser.add_argument("--head", default=os.environ.get("PRE_COMMIT_TO_REF"))
    parser.add_argument(
        "--pre-push",
        action="store_true",
        default="PRE_COMMIT_REMOTE_NAME" in os.environ,
        help="Require both push revisions.",
    )
    args = parser.parse_args()
    try:
        if args.pre_push and not (args.base and args.head):
            raise ValueError("--pre-push requires both --base and --head")
        findings = check(Path.cwd(), args.base, args.head)
    except (subprocess.CalledProcessError, ValueError):
        print(
            "reference-policy: cannot determine changes; check the base/head revisions",
            file=sys.stderr,
        )
        return 2
    for finding in findings:
        print(finding)
    if findings:
        print("Use generic names and synthetic examples. Do not add inline suppressions.")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
