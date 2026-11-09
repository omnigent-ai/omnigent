"""Check added text and paths against the repository reference policy."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

POLICY_FILES = frozenset({"dev/lint/lint_reference_policy.py"})
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


def violates(text: str) -> bool:
    """Match names in prose, snake_case, camelCase, and filenames."""
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", words)
    if PROHIBITED_NAMES.intersection(re.findall(r"[a-z]+", words.lower())):
        return True
    if PHRASES.search(words):
        return True
    return any(match.group().lower() not in PUBLIC_HOSTS for match in HOST.finditer(text))


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def check(root: Path, base: str | None = None, head: str | None = None) -> list[str]:
    """Check staged changes, or the branch diff from a merge base."""
    if head and not base:
        raise ValueError("--head requires --base")
    revisions = ["--cached"]
    if base:
        target = head or "HEAD"
        ancestor = git(root, "merge-base", base, target).decode().strip()
        revisions = [ancestor, target]
    options = ["--no-ext-diff", "--no-textconv", "--find-renames", *revisions]
    entries = iter(
        git(
            root, "diff", *options, "--raw", "--no-abbrev", "-z", "--diff-filter=AMRT", "--"
        ).split(b"\0")
    )
    findings = []
    for metadata in entries:
        if not metadata:
            break
        _, mode, _, object_id, status = metadata.split()
        old_path = os.fsdecode(next(entries))
        renamed = status.startswith(b"R")
        path = os.fsdecode(next(entries)) if renamed else old_path
        if path in POLICY_FILES:
            continue
        if (status == b"A" or renamed) and violates(path):
            findings.append(f"{path}:1: prohibited reference in new path")
        if mode == b"160000":
            continue
        contents = git(root, "cat-file", "blob", object_id.decode())
        if b"\0" in contents[:8000]:
            continue
        if old_path in POLICY_FILES:
            for line_number, line in enumerate(
                contents.decode("utf-8", errors="replace").split("\n"), 1
            ):
                if violates(line):
                    findings.append(f"{path}:{line_number}: prohibited reference in added text")
            continue
        paths = [old_path, path] if renamed else [path]
        patch = git(
            root,
            "--literal-pathspecs",
            "diff",
            *options,
            "--text",
            "--unified=0",
            "--",
            *paths,
        ).decode("utf-8", errors="replace")
        line_number = None
        for line in patch.split("\n"):
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                line_number = int(hunk[1])
            elif line_number is not None and line.startswith("+"):
                if violates(line[1:]):
                    findings.append(f"{path}:{line_number}: prohibited reference in added text")
                line_number += 1
            elif line_number is not None and line.startswith(" "):
                line_number += 1
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("PRE_COMMIT_FROM_REF"))
    parser.add_argument("--head", default=os.environ.get("PRE_COMMIT_TO_REF"))
    args = parser.parse_args()
    try:
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
