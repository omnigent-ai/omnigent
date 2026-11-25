"""Hides a shared session's pull request accounts and change controls from all but its owner."""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum
from typing import Any

from omnigent.runner.git_providers import ProviderCapabilities
from omnigent.runner.github_review_fields import (
    ACCOUNT_COMMENT_FIELDS,
    ACCOUNT_REACTION_FIELD,
    ACCOUNT_THREAD_FIELDS,
)

OWNER_ONLY_CHANGES_MESSAGE = (
    "Only the session's owner can change its pull requests. "
    "Open the pull request on its code host to make changes there."
)
# Capabilities that change the pull request, or the host's account and checkout.
OWNER_CAPABILITIES = frozenset(
    {
        "account_switching",
        "attachment_uploads",
        "base_remote_selection",
        "comment_posting",
        "details_editing",
        "label_editing",
        "merge_as_account",
        "merging",
        "review_thread_creation",
        "review_thread_replies",
        "reviewer_editing",
    }
)
# Capabilities that only read; everyone who sees the info keeps them.
READ_CAPABILITIES = frozenset(
    {"commit_history", "line_counts", "linked_pr_diff", "review_threads"}
)
# What a client assumes for a host that predates ``capabilities``.
_LEGACY_READ_CAPABILITIES = {"line_counts": True, "linked_pr_diff": True}
_ALL_CAPABILITIES = tuple(field.name for field in fields(ProviderCapabilities))


class PullRequestViewer(StrEnum):
    """Who reads a session's pull request info, decided once per request."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


def info_for_viewer(info: dict[str, Any], viewer: PullRequestViewer) -> dict[str, Any]:
    """``info`` as ``viewer`` may see it; the owner's is unchanged.

    :param info: A ``session.github.info`` payload, e.g. ``{"auth": {"accounts": [...]}}``.
    :param viewer: Who reads it.
    :returns: For anyone but the owner, a copy with ``owner_only_changes``, no account list
        and a full ``capabilities`` object whose change capabilities are all false.
    """
    if viewer is PullRequestViewer.OWNER:
        return info
    hidden = {"accounts"} | ({"selected_account"} if viewer is PullRequestViewer.VIEWER else set())
    shown: dict[str, Any] = {key: value for key, value in info.items() if key not in hidden}
    shown["owner_only_changes"] = True
    auth = info.get("auth")
    if isinstance(auth, dict):
        shown["auth"] = {**auth, **dict.fromkeys(hidden)}
    shown["capabilities"] = _read_only_capabilities(info.get("capabilities"))
    return shown


def _read_only_capabilities(capabilities: object) -> dict[str, bool]:
    """Every known capability, with only the reads kept; unknown names are turned off.

    Sent even when the host sent none, so a client's defaults for an older host
    can't offer a change the server refuses.
    """
    reported = capabilities if isinstance(capabilities, dict) else _LEGACY_READ_CAPABILITIES
    names = set(_ALL_CAPABILITIES) | {name for name in reported if isinstance(name, str)}
    return {
        name: name in READ_CAPABILITIES and reported.get(name) is True for name in sorted(names)
    }


# What the host's account may do in a review conversation, and with each comment in it.
# Hosts send whether it may reply even unasked, so that is hidden here too.
_THREAD_RIGHTS = ("can_reply", *ACCOUNT_THREAD_FIELDS)
_COMMENT_RIGHTS = ACCOUNT_COMMENT_FIELDS


def review_read_params(params: dict[str, Any], viewer: PullRequestViewer) -> dict[str, Any]:
    """A review read's host params: only the owner's ask for the host account's rights.

    A host sends them only when asked, so they never reach anyone else, whatever the
    shaping below does.
    """
    return {**params, "account_rights": True} if viewer is PullRequestViewer.OWNER else params


def review_threads_for_viewer(
    payload: dict[str, Any], viewer: PullRequestViewer
) -> dict[str, Any]:
    """A review-threads page as ``viewer`` may see it; the owner's is unchanged.

    :param payload: The host's page, e.g. ``{"data": [{"id": "T_1", "comments": {...}}]}``.
    :returns: For anyone but the owner, a copy whose threads and comments keep their IDs and
        reaction counts but say the host's account may do nothing with them.
    """
    if viewer is PullRequestViewer.OWNER:
        return payload
    return {**payload, "data": [_thread_for_others(item) for item in _items(payload)]}


def review_comments_for_viewer(
    payload: dict[str, Any], viewer: PullRequestViewer
) -> dict[str, Any]:
    """A page of one conversation's comments as ``viewer`` may see it; see above."""
    if viewer is PullRequestViewer.OWNER:
        return payload
    return {**payload, "data": [_comment_for_others(item) for item in _items(payload)]}


def _items(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _thread_for_others(thread: Any) -> Any:
    if not isinstance(thread, dict):
        return thread
    shown = {**thread, **dict.fromkeys(_THREAD_RIGHTS, False)}
    comments = thread.get("comments")
    if isinstance(comments, dict):
        shown["comments"] = review_comments_for_viewer(comments, PullRequestViewer.VIEWER)
    return shown


def _comment_for_others(comment: Any) -> Any:
    if not isinstance(comment, dict):
        return comment
    shown = {**comment, **dict.fromkeys(_COMMENT_RIGHTS, False)}
    reactions = comment.get("reactions")
    if isinstance(reactions, list):
        # The counts are everyone's; whether "you" reacted is the host account's.
        shown["reactions"] = [
            {**reaction, ACCOUNT_REACTION_FIELD: False} if isinstance(reaction, dict) else reaction
            for reaction in reactions
        ]
    return shown
