"""Check added text and paths against the repository reference policy."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

POLICY_FILES = frozenset(
    {
        "dev/lint/lint_reference_policy.py",
        "tests/dev/lint/test_lint_reference_policy.py",
    }
)
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
HOST = re.compile(
    r"(?<![\w.-])(?:[a-z0-9-]+\.)+(?:databricks\.com|databricksapps\.com|azuredatabricks\.net)(?![\w.-])",
    re.IGNORECASE,
)
SYNTHETIC_PREFIXES = frozenset(
    {
        "example",
        "test",
        "workspace",
        "ws",
        "acme",
        "account",
        "app.example",
        "dbc-test",
        "dbc-x",
        "dbc-123",
        "dbc-1234",
        "dbc-12345678-abcd",
        "dbc-87654321-dcba",
        "123",
        "1234567890123456",
        "4242424242424242",
    }
)
PUBLIC_HOSTS = frozenset(
    {
        "www.databricks.com",
        "docs.databricks.com",
        "accounts.cloud.databricks.com",
        "login.databricks.com",
    }
)


def violates(text: str) -> bool:
    """Match names in prose, snake_case, camelCase, and filenames."""
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", words)
    if PROHIBITED_NAMES.intersection(re.findall(r"[a-z]+", words.lower())):
        return True
    if PHRASES.search(words):
        return True
    for match in HOST.finditer(text):
        host = match.group().lower()
        if host in PUBLIC_HOSTS:
            continue
        prefix = re.sub(
            r"\.(?:(?:ai-gateway|staging|gcp)\.)?(?:cloud\.)?(?:databricks\.com|databricksapps\.com|azuredatabricks\.net)$",
            "",
            host,
        )
        if prefix not in SYNTHETIC_PREFIXES:
            return True
    return False


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
    options = ["--no-ext-diff", "--no-textconv", "--no-renames", *revisions]
    entries = git(root, "diff", *options, "--name-status", "-z", "--diff-filter=ACMT", "--").split(
        b"\0"
    )
    findings = []
    for index in range(0, len(entries) - 1, 2):
        status, raw_path = entries[index : index + 2]
        path = os.fsdecode(raw_path)
        if path in POLICY_FILES:
            continue
        if status == b"A" and violates(path):
            findings.append(f"{path}:1: prohibited reference in new path")
        patch = git(root, "diff", *options, "--unified=0", "--", path).decode(
            "utf-8", errors="replace"
        )
        line_number = None
        for line in patch.splitlines():
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
