#!/usr/bin/env python3
"""Compute a ``size/{XS,S,M,L,XL}`` label for a PR from its changed files.

Reads the GitHub ``pulls/{n}/files`` JSON array on stdin (objects with
``filename``, ``additions``, ``deletions``) and prints the size label. Lock
and generated files are excluded so a dependency bump does not inflate the
size. Pure stdlib so it runs without an install and is unit-tested directly.
"""

from __future__ import annotations

import json
import re
import sys

# Files whose churn should not count toward review size.
GENERATED = (
    re.compile(r"^uv\.lock$"),
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"(^|/)yarn\.lock$"),
)

# Upper bound (inclusive) of changed lines for each label, smallest first.
THRESHOLDS = (
    ("XS", 9),
    ("S", 49),
    ("M", 199),
    ("L", 499),
    ("XL", float("inf")),
)


def is_generated(filename: str) -> bool:
    return any(p.search(filename) for p in GENERATED)


def size_label(total: int) -> str:
    for name, upper in THRESHOLDS:
        if total <= upper:
            return f"size/{name}"
    raise AssertionError("THRESHOLDS must end with an unbounded bucket")


def total_changes(files: list[dict]) -> int:
    return sum(
        f.get("additions", 0) + f.get("deletions", 0)
        for f in files
        if not is_generated(f.get("filename", ""))
    )


def main() -> int:
    files = json.load(sys.stdin)
    print(size_label(total_changes(files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
