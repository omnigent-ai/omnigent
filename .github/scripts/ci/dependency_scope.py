#!/usr/bin/env python3
"""Select consumers of isolated dependency files; everything else runs full CI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

DEPENDENCY_SCOPES = {
    "dev/omnidev/Cargo.lock": "omnidev",
    "tests/codex_parity/sidecar/Cargo.lock": "sidecar",
    ".pre-commit-config.yaml": "hooks",
    "pnpm-lock.yaml": "javascript",
    "web/package.json": "javascript",
}


def select_scope(files: list[dict], *, reason: str = "") -> dict[str, str]:
    """Bundles retain the union of consumers; unknown or non-modified files expand it."""
    scopes = {DEPENDENCY_SCOPES.get(file["filename"], "full") for file in files}
    full = bool(reason) or not files or "full" in scopes
    full |= any(file["status"] != "modified" for file in files)
    if full:
        scopes = {"full"}
    ui = "full" if full or "javascript" in scopes else "sidecar" if "sidecar" in scopes else "none"
    return {
        "backend": str(full).lower(),
        "omnidev": str(full or "omnidev" in scopes).lower(),
        "sidecar": str(full or "sidecar" in scopes).lower(),
        "ui": ui,
        "reason": reason or f"Dependency scopes: {', '.join(sorted(scopes))}",
    }


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, timeout=30).strip("\n")


def pr_files(event: dict, revision: str) -> list[dict]:
    """Diff the exact tested merge against its base, without a moving API inventory."""
    parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()
    if (
        len(parents) != 3
        or parents[0] != revision
        or parents[2] != event["pull_request"]["head"]["sha"]
    ):
        raise ValueError("Checkout is not the event's PR merge revision")
    changes = git("diff", "--no-renames", "--name-status", "-z", parents[1], parents[0])
    fields = changes.rstrip("\0").split("\0") if changes else []
    return [
        {"filename": name, "status": "modified" if status == "M" else status}
        for status, name in zip(fields[::2], fields[1::2], strict=True)
    ]


def main() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        selection = select_scope([], reason="Push, scheduled and manual runs use full CI")
    else:
        try:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            selection = select_scope(pr_files(event, os.environ["GITHUB_SHA"]))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            print(f"Cannot determine dependency scope ({type(exc).__name__}); running full CI")
            selection = select_scope([], reason="Scope unavailable; running full CI")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in selection.items():
            output.write(f"{key}={value}\n")
    print(json.dumps(selection, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write("### Dependency CI selection\n\n```json\n")
            output.write(json.dumps(selection, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
