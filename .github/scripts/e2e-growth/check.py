#!/usr/bin/env python3
"""Count net-new e2e tests in a PR and emit a growth verdict.

Reads the PR's changed-files JSON (the GitHub `pulls/{n}/files` payload) on
stdin, counts added `def test_` lines and net-new test files under the e2e
suites, and writes a warning comment body to $COMMENT_FILE. Prints a compact
JSON verdict to stdout for the workflow to act on. Warn-only: the caller never
fails the PR on this -- it nudges authors and keeps the growth visible so a
big batch of new e2e tests is a deliberate, reviewed decision (see OMNI-6715).

Pure computation from the diff text -- no checkout of PR code -- so it is safe
to run from the default branch under pull_request_target.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Suites whose growth drives the sharded e2e / e2e-ui CI wall-clock. Kept to the
# two heavy Playwright/mock-LLM suites; tests/e2e_live is a single smoke test.
E2E_DIRS = {
    "tests/e2e/": "e2e",
    "tests/e2e_ui/": "e2e_ui",
}

# Warn (comment + label) above either threshold, per PR. Tuned to flag a large
# batch, not the routine one-or-two-test PR. Bump down to tighten, or gate the
# workflow step on `over` to make it blocking.
NEW_TESTS_THRESHOLD = 20
NEW_FILES_THRESHOLD = 8

MARKER = "<!-- e2e-growth-guard -->"

# An added source line that defines a test function (sync or async).
_ADDED_TEST_DEF = re.compile(r"^\+\s*(async\s+)?def test_", re.MULTILINE)


def _suite_of(filename: str) -> str | None:
    for prefix, name in E2E_DIRS.items():
        if filename.startswith(prefix):
            return name
    return None


def _is_test_file(filename: str) -> bool:
    base = filename.rsplit("/", 1)[-1]
    return base.startswith("test_") and base.endswith(".py")


def main() -> int:
    files = json.load(sys.stdin)

    added_tests = {"e2e": 0, "e2e_ui": 0}
    new_files = {"e2e": 0, "e2e_ui": 0}

    for f in files:
        suite = _suite_of(f.get("filename", ""))
        if suite is None or not _is_test_file(f["filename"]):
            continue
        if f.get("status") == "added":
            new_files[suite] += 1
        # `patch` is absent for binary/too-large diffs; skip those gracefully.
        patch = f.get("patch") or ""
        added_tests[suite] += len(_ADDED_TEST_DEF.findall(patch))

    total_tests = sum(added_tests.values())
    total_files = sum(new_files.values())
    over = total_tests > NEW_TESTS_THRESHOLD or total_files > NEW_FILES_THRESHOLD

    comment_path = os.environ.get("COMMENT_FILE")
    if comment_path and over:
        with open(comment_path, "w") as fh:
            fh.write(_comment_body(added_tests, new_files, total_tests, total_files))

    json.dump(
        {
            "over": over,
            "added_tests": total_tests,
            "new_files": total_files,
            "e2e_tests": added_tests["e2e"],
            "e2e_ui_tests": added_tests["e2e_ui"],
            "e2e_files": new_files["e2e"],
            "e2e_ui_files": new_files["e2e_ui"],
        },
        sys.stdout,
    )
    return 0


def _comment_body(added_tests, new_files, total_tests, total_files) -> str:
    thresholds = (
        f"Thresholds: >{NEW_TESTS_THRESHOLD} added tests or "
        f">{NEW_FILES_THRESHOLD} new files per PR. Context: OMNI-6715."
    )
    return f"""{MARKER}
### 🧪 E2E test growth

This PR adds a large batch of end-to-end tests:

| Suite | New test files | Added `def test_` |
|---|---|---|
| `tests/e2e/` | {new_files["e2e"]} | {added_tests["e2e"]} |
| `tests/e2e_ui/` | {new_files["e2e_ui"]} | {added_tests["e2e_ui"]} |
| **Total** | **{total_files}** | **{total_tests}** |

The e2e / e2e-ui suites run on nearly every PR and are sharded under a fixed
wall-clock cap, so each new test adds directly to CI latency for everyone. This
is a **warning, not a blocker** — please just confirm the additions are
necessary and consider:

- Folding assertions into an existing test instead of a new file/function.
- `@pytest.mark.nightly` for slow paths that don't need to gate every PR.
- A cheaper unit/integration test where full e2e isn't required.

{thresholds}
"""


if __name__ == "__main__":
    sys.exit(main())
