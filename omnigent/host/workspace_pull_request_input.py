"""Checks on a pull request call's own fields, which the server and the host both make.

The server refuses such a call before it builds a vouch or sends a frame. The host
refuses it again, so a frame from any server is held to the same rules.
"""

from __future__ import annotations

from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.runner import pr_resource
from omnigent.runner.github_lookup import MAX_PULL_REQUEST_URLS
from omnigent.runner.github_review_fields import (
    COMMENT_KINDS,
    COMMENT_REVIEW_EVENT,
    NODE_ID,
    PENDING_EVENTS,
    REACTION_CONTENTS,
    SHA256,
    VERDICT_STATES,
)
from omnigent.runner.github_search import MAX_REVIEWERS
from omnigent.runner.pr_actions import AUTO_MERGE_BATCH_LIMIT

# The largest pull request attachment, a video; the runner checks each type's own limit.
# The call frame carries it base64-encoded, a third larger, under the tunnel's message cap.
MAX_ATTACHMENT_BYTES = pr_resource.MAX_VIDEO_ATTACHMENT_BYTES
# Caps on an upload's other fields, so the largest allowed call still fits one frame.
MAX_ATTACHMENT_NAME_CHARACTERS = 255
MAX_PULL_REQUEST_URL_CHARACTERS = 2048
# GitHub label names are at most 50 characters; a search needs no more than a few.
MAX_LABEL_SEARCH_CHARACTERS = 256
# A login or team name is far shorter; the same cap bounds the other searches.
MAX_SEARCH_CHARACTERS = MAX_LABEL_SEARCH_CHARACTERS
_COMPLETION_KINDS = frozenset({pb.COMPLETION_KIND_MENTION, pb.COMPLETION_KIND_REFERENCE})
MAX_IMAGE_URL_CHARACTERS = 2048
_LONG_PULL_REQUEST_URL = (
    f"The pull request URL is longer than {MAX_PULL_REQUEST_URL_CHARACTERS} characters"
)

# Each review method's allowed events; the others carry none.
_EVENTS: dict[str, frozenset[str]] = {
    "SubmitReview": frozenset({COMMENT_REVIEW_EVENT}),
    "SubmitReviewVerdict": frozenset(VERDICT_STATES),
    "SubmitPendingReview": PENDING_EVENTS,
}
# GitHub node IDs a request names, and whether each method requires it.
_NODE_ID_FIELDS: dict[str, dict[str, bool]] = {
    "AddPendingReviewComment": {"pending_review_id": True, "in_reply_to_thread_id": False},
    "UpdatePendingReviewComment": {"pending_review_id": True, "comment_node_id": True},
    "DeletePendingReviewComment": {"pending_review_id": True, "comment_node_id": True},
    "SubmitPendingReview": {"pending_review_id": True},
    "DiscardPendingReview": {"pending_review_id": True},
    "SetReviewThreadResolved": {"thread_id": True},
}
_COMMENT_METHODS = frozenset({"EditComment", "DeleteComment", "AddReaction", "RemoveReaction"})
_DIGEST_METHODS = frozenset({"EditComment", "UpdatePendingReviewComment"})
INVALID_INPUT_REASON = "invalid_input"


class PullRequestRefused(ConnectError):
    """A refused pull request call, with the machine-readable reason a client reads.

    :ivar reason: e.g. ``"comment_changed"``, or ``None`` when there's no reason.
    """

    def __init__(self, code: Code, message: str, reason: str | None) -> None:
        super().__init__(code, message)
        self.reason = reason


def _auto_merge_refusal(request: Any) -> str | None:
    """Why an auto-merge batch can't be vouched for, before the server checks each PR."""
    if not 1 <= len(request.pull_requests) <= AUTO_MERGE_BATCH_LIMIT:
        return f"Enable auto-merge for 1 to {AUTO_MERGE_BATCH_LIMIT} pull requests at once"
    for item in request.pull_requests:
        if not item.pr_url:
            return "Name each pull request to merge automatically"
        if len(item.pr_url) > MAX_PULL_REQUEST_URL_CHARACTERS:
            return (
                f"The pull request URL is longer than {MAX_PULL_REQUEST_URL_CHARACTERS} characters"
            )
    return None


def refuse_unserved_request(method: str, request: Any) -> None:
    """Refuse a request for something this API doesn't do, before any other work.

    :param method: The method's name, e.g. ``"SetPullRequestAccount"``.
    :param request: Its request message.
    :raises PullRequestRefused: INVALID_ARGUMENT, with the message and ``invalid_input``.
    """
    reason: str | None = None
    if method == "SetPullRequestAccount":
        # Without a PR the account and remote would change for the whole workspace.
        if not request.pr_url:
            reason = "Expected a pull request URL"
        elif request.remote:
            reason = "Changing the base repository isn't available here"
    elif method == "MergePullRequestAsAccount":
        # Without an account the merge would run as the saved one, not the confirmed one.
        if not request.account.strip():
            reason = "Expected an account to merge as"
    elif method == "ListLabels" and len(request.text) > MAX_LABEL_SEARCH_CHARACTERS:
        reason = f"The label search is longer than {MAX_LABEL_SEARCH_CHARACTERS} characters"
    elif method in {"SearchReviewers", "SearchCompletions"}:
        reason = _search_refusal(method, request)
    elif method == "LookupPullRequest":
        reason = _named_url_refusal(request.pr_url)
    elif method == "GetPullRequestListStats":
        reason = _list_stats_refusal(request.pr_urls)
    elif method == "EnableAutoMerge":
        reason = _auto_merge_refusal(request)
    elif method == "ReadPullRequestImage":
        if not request.image_url:
            reason = "Expected an image URL"
        elif len(request.image_url) > MAX_IMAGE_URL_CHARACTERS:
            reason = f"The image URL is longer than {MAX_IMAGE_URL_CHARACTERS} characters"
        elif len(request.pr_url) > MAX_PULL_REQUEST_URL_CHARACTERS:
            reason = _LONG_PULL_REQUEST_URL
    elif method == "UploadAttachment":
        if not request.name.strip():
            reason = "Expected the file's name"
        elif len(request.name) > MAX_ATTACHMENT_NAME_CHARACTERS:
            reason = f"The file's name is longer than {MAX_ATTACHMENT_NAME_CHARACTERS} characters"
        elif len(request.pr_url) > MAX_PULL_REQUEST_URL_CHARACTERS:
            reason = _LONG_PULL_REQUEST_URL
        elif not request.content:
            reason = "The attachment is empty"
        elif len(request.content) > MAX_ATTACHMENT_BYTES:
            reason = f"The attachment is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB"
    else:
        reason = _review_refusal(method, request)
    if reason is not None:
        raise PullRequestRefused(Code.INVALID_ARGUMENT, reason, INVALID_INPUT_REASON)


def _search_refusal(method: str, request: Any) -> str | None:
    """Why a reviewer or completion search's fields are malformed, or ``None``."""
    if len(request.text) > MAX_SEARCH_CHARACTERS:
        return f"The search is longer than {MAX_SEARCH_CHARACTERS} characters"
    if len(request.pr_url) > MAX_PULL_REQUEST_URL_CHARACTERS:
        return _LONG_PULL_REQUEST_URL
    if method == "SearchReviewers" and not 0 <= request.page_size <= MAX_REVIEWERS:
        return f"Ask for at most {MAX_REVIEWERS} reviewers"
    if method == "SearchCompletions" and request.kind not in _COMPLETION_KINDS:
        return "Choose what to complete: people or pull requests"
    return None


def _named_url_refusal(pr_url: str) -> str | None:
    if not pr_url:
        return "Expected a pull request URL"
    if len(pr_url) > MAX_PULL_REQUEST_URL_CHARACTERS:
        return _LONG_PULL_REQUEST_URL
    return None


def _list_stats_refusal(pr_urls: Any) -> str | None:
    """Why a list-stats read can't be sent: no URLs, too many, or one empty or too long."""
    if not 1 <= len(pr_urls) <= MAX_PULL_REQUEST_URLS:
        return f"Ask for 1 to {MAX_PULL_REQUEST_URLS} pull requests"
    return next(filter(None, (_named_url_refusal(url) for url in pr_urls)), None)


def _review_refusal(method: str, request: Any) -> str | None:
    """Why a review or comment change's fields are malformed, or ``None``."""
    if method in _EVENTS and request.event not in _EVENTS[method]:
        return f"Expected event to be one of {', '.join(sorted(_EVENTS[method]))}"
    for field, required in _NODE_ID_FIELDS.get(method, {}).items():
        value = getattr(request, field)
        if (value or required) and not NODE_ID.fullmatch(value):
            return f"{field} isn't a GitHub ID"
    if method in _COMMENT_METHODS:
        if request.kind not in COMMENT_KINDS:
            return "Expected kind to be issue or review"
        if request.comment_id < 1:
            return "Expected a comment id"
    if method in {"AddReaction", "RemoveReaction"} and request.content not in REACTION_CONTENTS:
        return "Expected a GitHub reaction, such as THUMBS_UP"
    if method in _DIGEST_METHODS and not SHA256.fullmatch(request.expected_body_sha256):
        return "Expected the text's SHA-256"
    return None
