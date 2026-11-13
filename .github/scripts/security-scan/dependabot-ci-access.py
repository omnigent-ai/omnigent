#!/usr/bin/env python3
"""Allow verified Dependabot compatibility CI after checking changed Actions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dependabot_automerge import evaluate
from dependabot_policy import require
from maintainer_approval import gh_json, paginated


def main() -> int:
    try:
        repository, number, head = (
            os.environ["REPO"],
            int(os.environ["PR"]),
            os.environ["HEAD_SHA"],
        )
        pull = gh_json(["api", f"repos/{repository}/pulls/{number}"])
        require(
            pull["user"]["login"] == "dependabot[bot]" and pull["user"]["type"] == "Bot",
            "Not a verified Dependabot PR",
        )
        require(pull["head"]["repo"]["full_name"] == repository, "Fork PR requires scanning")
        require(pull["head"]["sha"] == head, "PR head changed during evaluation")
        if any(label["name"] == "skip-security-scan" for label in pull["labels"]):
            print(
                "::notice::'skip-security-scan' maintainer waiver: CI pin-only validation waived; "
                "unattended merge remains disabled."
            )
            return 0
        files = paginated(f"repos/{repository}/pulls/{number}/files")
        require(bool(files) and len(files) == pull["changed_files"], "Incomplete file inventory")
        if any(
            file["filename"].startswith((".github/workflows/", ".github/actions/"))
            or file.get("previous_filename", "").startswith(
                (".github/workflows/", ".github/actions/")
            )
            for file in files
        ):
            evaluate(repository, number, head, for_scan=True)
            print("Changed Actions passed structural and release pin checks.")
        print("Verified Dependabot compatibility CI may proceed; Security Scan still gates merge.")
        return 0
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::Dependabot CI access denied: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
