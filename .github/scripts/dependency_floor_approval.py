#!/usr/bin/env python3
"""Require a second maintainer for a new young-dependency-floor waiver.

A waiver comment in pyproject.toml is self-attested, so a PR that newly waives a
requirement (compared with its merge base) also needs a maintainer other than
the author to add the approval label and approve a commit carrying that waiver.
Binding the label to an approved commit keeps a later edit from riding on it.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from maintainer_approval import gh_json, latest_decisive_reviews, maintainers, paginated

from dev.lint._framework import disabled_rules_by_line

RULE_NAME = "young-dependency-floor"
LABEL = "dependency-floor-approved"
PYPROJECT = "pyproject.toml"


@dataclass(frozen=True)
class FloorDecision:
    approved: bool
    reason: str


def waived_requirements(pyproject_text: str) -> set[str]:
    """Stripped text of every line a young-dependency-floor waiver covers."""
    lines = pyproject_text.splitlines()
    return {
        lines[number - 1].strip()
        for number, rules in disabled_rules_by_line(pyproject_text).items()
        if RULE_NAME in rules and number <= len(lines)
    }


def floor_decision(
    *,
    author: str,
    labels: set[str],
    new_waivers: set[str],
    maintainer_logins: set[str],
    timeline: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    waivers_at: Callable[[str], set[str]],
) -> FloorDecision:
    """Accept new waivers only with the label and approval of a non-author maintainer."""
    if not new_waivers:
        return FloorDecision(True, "No young-dependency-floor waiver added or changed.")
    listed = ", ".join(f"`{waiver}`" for waiver in sorted(new_waivers))
    ask = (
        f"{PYPROJECT} adds or changes a {RULE_NAME} waiver on {listed}. A maintainer other "
        f"than @{author} should confirm the release is on the internal mirror "
        f"(`just check-internal-mirror`), add the `{LABEL}` label, and approve the PR."
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
    review = {
        login.casefold(): review for login, review in latest_decisive_reviews(reviews).items()
    }.get(labeler.casefold())
    if review is None or str(review.get("state") or "").upper() != "APPROVED":
        return FloorDecision(False, f"@{labeler} added `{LABEL}` but has not approved. {ask}")
    approved_sha = str(review.get("commit_id") or "")
    if not new_waivers <= waivers_at(approved_sha):
        return FloorDecision(
            False, f"@{labeler} approved {approved_sha[:9]}, before these waivers. {ask}"
        )
    return FloorDecision(
        True, f"Waivers on {listed} approved by maintainer @{labeler} at {approved_sha[:9]}."
    )


def evaluate(
    repository: str,
    pr_number: int,
    request: Callable[[list[str]], Any] = gh_json,
    maintainer_lookup: Callable[[str], set[str]] = maintainers,
) -> FloorDecision:
    pull = request(["api", f"repos/{repository}/pulls/{pr_number}"])
    head_sha = pull["head"]["sha"]
    compare = request(
        ["api", f"repos/{repository}/compare/{pull['base']['sha']}...{head_sha}?per_page=1"]
    )

    def waivers_at(sha: str) -> set[str]:
        content = request(["api", f"repos/{repository}/contents/{PYPROJECT}?ref={sha}"])
        return waived_requirements(base64.b64decode(content["content"]).decode())

    head_waivers = waivers_at(head_sha)
    new_waivers = head_waivers - waivers_at(compare["merge_base_commit"]["sha"])
    if not new_waivers:
        return FloorDecision(True, "No young-dependency-floor waiver added or changed.")
    return floor_decision(
        author=str((pull.get("user") or {}).get("login") or ""),
        labels={str(label.get("name") or "") for label in pull.get("labels") or []},
        new_waivers=new_waivers,
        maintainer_logins=maintainer_lookup(repository),
        timeline=paginated(f"repos/{repository}/issues/{pr_number}/timeline", request),
        reviews=paginated(f"repos/{repository}/pulls/{pr_number}/reviews", request),
        waivers_at=lambda sha: head_waivers if sha == head_sha else waivers_at(sha),
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
