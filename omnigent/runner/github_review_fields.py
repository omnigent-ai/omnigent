"""The values GitHub review and comment changes accept, and the comment fields they rely on.

Pure, so the server can check a request with them before it forwards it.
"""

from __future__ import annotations

import re
from typing import Any

# The verdicts a review can carry, and the state GitHub reports for each once submitted.
VERDICT_STATES = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
COMMENT_REVIEW_EVENT = "COMMENT"
PENDING_EVENTS = frozenset({COMMENT_REVIEW_EVENT, *VERDICT_STATES})
REACTION_CONTENTS = frozenset(
    {"THUMBS_UP", "THUMBS_DOWN", "LAUGH", "HOORAY", "CONFUSED", "HEART", "ROCKET", "EYES"}
)
COMMENT_KINDS = frozenset({"issue", "review"})
SIDES = frozenset({"LEFT", "RIGHT"})
SHA256 = re.compile(r"[0-9a-f]{64}")
# GitHub's opaque node IDs, such as a review thread's, a draft's or a pending review's.
NODE_ID = re.compile(r"[A-Za-z0-9_=-]{1,200}")


def database_id(value: Any) -> int | None:
    """A comment's numeric ID, which its edit, delete and reaction calls name; None if unknown."""
    text = str(value) if isinstance(value, int | str) else ""
    return int(text) if text.isdigit() and int(text) > 0 else None


def reactions(groups: Any) -> list[dict[str, Any]]:
    """A comment's reactions with at least one person, and whether the viewer left each."""
    shaped = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or not isinstance(group.get("content"), str):
            continue
        reactors = group.get("reactors")
        count = reactors.get("totalCount") if isinstance(reactors, dict) else None
        if isinstance(count, int) and count > 0:
            shaped.append(
                {
                    "content": group["content"],
                    "count": count,
                    "viewer_reacted": bool(group.get("viewerHasReacted")),
                }
            )
    return shaped
