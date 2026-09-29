#!/usr/bin/env python3
"""Skip product suites only for a complete, unchanged PR diff of known automation files."""

import json
import os
import subprocess
from pathlib import Path

POLLY_TESTS = {
    f"tests/scripts/test_polly_review_{name}.py"
    for name in ("summary", "prompt", "output", "trigger")
}
POLLY_FILES = POLLY_TESTS | {
    ".github/scripts/polly-review-summary.py",
    ".github/workflows/polly-review.yml",
}


def gh_json(endpoint: str):
    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(result.stdout)


def pr_version(pr: dict) -> tuple:
    return pr["base"]["sha"], pr["head"]["sha"], pr["changed_files"]


def changed_paths(event: dict, api=gh_json) -> set[str]:
    pr = event["pull_request"]
    endpoint = f"repos/{event['repository']['full_name']}/pulls/{pr['number']}"
    version = pr_version(pr)
    count = version[2]
    if not isinstance(count, int) or not 0 < count < 3000:
        raise ValueError("empty or potentially truncated diff")
    if pr_version(api(endpoint)) != version:
        raise ValueError("PR changed since this workflow event")

    files = []
    for page in range(1, (count + 99) // 100 + 1):
        batch = api(f"{endpoint}/files?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("invalid files response")
        files.extend(batch)
    if len(files) != count or len({f["filename"] for f in files}) != count:
        raise ValueError("incomplete diff")
    if pr_version(api(endpoint)) != version:
        raise ValueError("PR changed while reading its diff")

    paths = set()
    for file in files:
        paths.add(file["filename"])
        if file["status"] == "renamed":
            paths.add(file["previous_filename"])
        elif file["status"] not in {"added", "modified", "removed"}:
            raise ValueError("unknown file status")
    if any(not isinstance(path, str) or not path for path in paths):
        raise ValueError("invalid file path")
    return paths


def select(event_name: str, event: dict, api=gh_json) -> tuple[bool, str]:
    if event_name != "pull_request":
        return True, "Full suites for push, schedule, and manual runs."
    try:
        paths = changed_paths(event, api)
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return True, "Full suites: diff unavailable, incomplete, or changed since the event."
    if paths & POLLY_FILES and paths <= POLLY_FILES | {"CHANGELOG.md"}:
        return False, "Polly review automation only: run its script tests; skip product suites."
    return True, "Full suites: diff includes files outside the automation allowlist."


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    except (KeyError, ValueError, OSError):
        event = {}
    product, reason = select(os.environ.get("GITHUB_EVENT_NAME", ""), event)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"product={str(product).lower()}\n")
    print(reason)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write(f"## Test suite selection\n\n{reason}\n")


if __name__ == "__main__":
    main()
