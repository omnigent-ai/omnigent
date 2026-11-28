"""The values GitHub review and comment changes accept, and the comment fields they rely on.

Pure, so the server can check a request with them before it forwards it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


# The largest whole number a JSON or protobuf Struct number (a double) carries exactly.
MAX_EXACT_ID = 2**53 - 1


def database_id(value: Any) -> int | None:
    """A comment's numeric ID, which its edit, delete and reaction calls name; None if unknown.

    An ID a double can't carry exactly is None too, so a client never names another comment.
    """
    text = str(value) if isinstance(value, int | str) else ""
    number = int(text) if text.isdigit() else 0
    return number if 0 < number <= MAX_EXACT_ID else None


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


# What GitHub says the host's account may do in a review conversation, or did there. A server
# hides them from all but the session's owner, so the host sends them only when it asks.
ACCOUNT_THREAD_FIELDS = ("can_resolve", "can_unresolve")
ACCOUNT_COMMENT_FIELDS = ("can_react", "can_update", "can_delete", "viewer_did_author")
ACCOUNT_REACTION_FIELD = "viewer_reacted"


def threads_without_account_rights(page: dict[str, Any]) -> dict[str, Any]:
    """A review-threads page without its account fields; IDs, content and counts stay."""
    return {**page, "data": _mapped(page.get("data"), _thread_without_account_rights)}


def comments_without_account_rights(page: dict[str, Any]) -> dict[str, Any]:
    """A page of one conversation's comments without its account fields; see above."""
    return {**page, "data": _mapped(page.get("data"), _comment_without_account_rights)}


def info_without_account_rights(info: dict[str, Any]) -> dict[str, Any]:
    """A ``session.github.info`` payload whose conversation comments lack the account fields."""
    pr = info.get("pr")
    if not isinstance(pr, dict) or not isinstance(pr.get("comments"), list):
        return info
    comments = _mapped(pr["comments"], _comment_without_account_rights)
    return {**info, "pr": {**pr, "comments": comments}}


def _mapped(items: Any, shape: Callable[[Any], Any]) -> Any:
    return [shape(item) for item in items] if isinstance(items, list) else items


def _thread_without_account_rights(thread: Any) -> Any:
    if not isinstance(thread, dict):
        return thread
    shown = {key: value for key, value in thread.items() if key not in ACCOUNT_THREAD_FIELDS}
    comments = thread.get("comments")
    if isinstance(comments, dict):
        shown["comments"] = comments_without_account_rights(comments)
    return shown


def _comment_without_account_rights(comment: Any) -> Any:
    if not isinstance(comment, dict):
        return comment
    shown = {key: value for key, value in comment.items() if key not in ACCOUNT_COMMENT_FIELDS}
    reactions = comment.get("reactions")
    if isinstance(reactions, list):
        shown["reactions"] = [
            {key: value for key, value in reaction.items() if key != ACCOUNT_REACTION_FIELD}
            if isinstance(reaction, dict)
            else reaction
            for reaction in reactions
        ]
    return shown
