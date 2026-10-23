#!/usr/bin/env python3
"""Require a second maintainer's label on a young-dependency-floor waiver.

The young-dependency-floor lint can be waived in pyproject.toml for an urgent
bump the internal mirrors already serve. The waiver comment is self-attested,
so a PR that adds or changes one (the directive or the requirement it covers)
also needs the approval label, added by a maintainer other than the author after
running ``just check-internal-mirror``. Waivers are parsed with the lint's own
``disabled_rules_by_line`` so both sides agree on what a waiver is.
"""

from __future__ import annotations

import argparse
import base64
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from maintainer_approval import gh_json, maintainers, paginated

from dev.lint._framework import disabled_rules_by_line

RULE_NAME = "young-dependency-floor"
LABEL = "dependency-floor-approved"
PYPROJECT = "pyproject.toml"
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class FloorDecision:
    approved: bool
    reason: str


def waiver_lines(pyproject_text: str) -> set[int]:
    """Line numbers holding a waiver directive or the requirement it covers."""
    covered = {
        line
        for line, rules in disabled_rules_by_line(pyproject_text).items()
        if RULE_NAME in rules
    }
    directives = {
        number
        for number, text in enumerate(pyproject_text.splitlines(), start=1)
        if any(RULE_NAME in rules for rules in disabled_rules_by_line(text).values())
    }
    return covered | directives


def added_lines(patch: str) -> set[int]:
    """New-file line numbers a unified diff adds or rewrites."""
    added: set[int] = set()
    line = 0
    for text in patch.splitlines():
        if hunk := _HUNK_RE.match(text):
            line = int(hunk.group(1))
        elif text.startswith("+"):
            added.add(line)
            line += 1
        elif text.startswith(" "):
            line += 1
    return added


def floor_decision(
    *,
    author: str,
    labels: set[str],
    changed_waivers: list[int],
    maintainer_logins: set[str],
    timeline: list[dict[str, Any]],
) -> FloorDecision:
    """Accept a waiver change only with the label from a non-author maintainer."""
    lines = ", ".join(str(line) for line in changed_waivers)
    ask = (
        f"{PYPROJECT} line(s) {lines} add or change a {RULE_NAME} waiver. A maintainer "
        f"other than @{author} should confirm the release is on the internal mirror "
        f"(`just check-internal-mirror`) and add the `{LABEL}` label."
    )
    if LABEL not in labels:
        return FloorDecision(False, ask)
    labelers = [
        str((event.get("actor") or {}).get("login") or "")
        for event in timeline
        if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == LABEL
    ]
    labeler = labelers[-1] if labelers else "unknown"
    if labeler.casefold() == author.casefold():
        return FloorDecision(False, f"`{LABEL}` was added by the author. {ask}")
    if labeler.casefold() not in {login.casefold() for login in maintainer_logins}:
        return FloorDecision(False, f"`{LABEL}` was added by non-maintainer @{labeler}. {ask}")
    return FloorDecision(True, f"Waiver on line(s) {lines} approved by maintainer @{labeler}.")


def evaluate(
    repository: str,
    pr_number: int,
    request: Callable[[list[str]], Any] = gh_json,
    maintainer_lookup: Callable[[str], set[str]] = maintainers,
) -> FloorDecision:
    pull = request(["api", f"repos/{repository}/pulls/{pr_number}"])
    files = paginated(f"repos/{repository}/pulls/{pr_number}/files", request)
    entry = next((item for item in files if item.get("filename") == PYPROJECT), None)
    if entry is None or entry.get("status") == "removed":
        return FloorDecision(True, f"{PYPROJECT} is unchanged.")
    head_sha = pull["head"]["sha"]
    content = request(["api", f"repos/{repository}/contents/{PYPROJECT}?ref={head_sha}"])
    waivers = waiver_lines(base64.b64decode(content["content"]).decode())
    # GitHub omits the patch for very large diffs; treat every waiver as changed.
    patch = entry.get("patch")
    changed = sorted(waivers if patch is None else waivers & added_lines(patch))
    if not changed:
        return FloorDecision(True, "No young-dependency-floor waiver added or changed.")
    return floor_decision(
        author=str((pull.get("user") or {}).get("login") or ""),
        labels={str(label.get("name") or "") for label in pull.get("labels") or []},
        changed_waivers=changed,
        maintainer_logins=maintainer_lookup(repository),
        timeline=paginated(f"repos/{repository}/issues/{pr_number}/timeline", request),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    args = parser.parse_args()
    decision = evaluate(args.repository, args.pr_number)
    print(decision.reason if decision.approved else f"::error::{decision.reason}")
    return 0 if decision.approved else 1


if __name__ == "__main__":
    raise SystemExit(main())
