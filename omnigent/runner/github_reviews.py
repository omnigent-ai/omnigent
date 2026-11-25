"""GitHub reviews, pending reviews, review conversations, and comment edits and reactions.

Every write first checks that what it changes is on the session's pull request and, for
comments, that the host's account wrote it. A write GitHub never confirmed is an unknown
outcome (see :mod:`omnigent.runner.github_failures`), never a refusal.
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

from omnigent.runner import github_failures
from omnigent.runner import github_pending_reviews as pending_reviews
from omnigent.runner import github_resource as github
from omnigent.runner.github_graphql import (
    AUTHENTICATION_TIMED_OUT,
    mutate,
    pull_request_node,
    require_head,
)
from omnigent.runner.github_review_fields import (
    COMMENT_KINDS,
    NODE_ID,
    REACTION_CONTENTS,
    SHA256,
    VERDICT_STATES,
    reactions,
)
from omnigent.runner.github_review_threads import _graphql
from omnigent.runner.pull_request_errors import (
    CommentChangedError,
    InvalidPullRequestInput,
    PullRequestChangedError,
    PullRequestError,
)
from omnigent.runner.session_prs import PullRequestRef

_Action = TypeVar("_Action", bound=Callable[..., dict[str, Any]])


def _as_one_account(action: _Action) -> _Action:
    """Run the action's reads and its write as the one account chosen when it starts."""

    @functools.wraps(action)
    def run(root: str, reference: PullRequestRef, *args: Any, **kwargs: Any) -> dict[str, Any]:
        with github._one_pr_account(root, reference, timed_out=AUTHENTICATION_TIMED_OUT):
            return action(root, reference, *args, **kwargs)

    return cast("_Action", run)


_REVIEW_MUTATION = """mutation($input: AddPullRequestReviewInput!) {
  addPullRequestReview(input: $input) { pullRequestReview { id state } }
}"""
_SUBMITTED_STATES = {"COMMENT": "COMMENTED", **VERDICT_STATES}


@_as_one_account
def submit_review(
    root: str, reference: PullRequestRef, *, event: str, body: str, commit_oid: str
) -> dict[str, Any]:
    """Submit a review with ``event`` in one step, which GitHub refuses if the head moved."""
    pull_request = require_head(root, reference, commit_oid)
    payload = mutate(
        root,
        reference,
        _REVIEW_MUTATION,
        "addPullRequestReview",
        action="submit this review",
        expected_head_oid=commit_oid,
        input={
            "pullRequestId": pull_request["id"],
            "commitOID": commit_oid,
            "event": event,
            "body": body,
        },
    )
    review = payload.get("pullRequestReview") or {}
    if review.get("state") != _SUBMITTED_STATES[event]:
        raise github_failures.unknown_outcome()
    return {"object": "session.github.pr_review", "pr_url": reference.url, "event": event}


def _pending_mutate(root: str, reference: PullRequestRef, *args: Any, **kwargs: Any) -> Any:
    """:func:`mutate` on the pending review; its kept read goes whatever the outcome."""
    try:
        return mutate(root, reference, *args, **kwargs)
    finally:
        pending_reviews.forget_pending_review(reference)


def _confirm_review(payload: dict[str, Any], review_id: str) -> None:
    """Refuse to report success when GitHub's answer names another review than ``review_id``.

    A deletion may answer with no review, so only a review that's there is checked.
    """
    review = payload.get("pullRequestReview")
    if review is not None and (not isinstance(review, dict) or review.get("id") != review_id):
        raise github_failures.unknown_outcome()


@_as_one_account
def get_pending_review(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """The viewer's pending review and its drafts, in the page's shape; see the module."""
    return pending_reviews.get_pending_review(root, reference)


_START_MUTATION = """mutation($input: AddPullRequestReviewInput!) {
  addPullRequestReview(input: $input) {
    pullRequestReview { id state viewerDidAuthor commit { oid } }
  }
}"""


@_as_one_account
def start_pending_review(
    root: str, reference: PullRequestRef, *, commit_oid: str
) -> dict[str, Any]:
    """Start a pending review on ``commit_oid``, while it is still the head; answer only it."""
    pull_request = require_head(root, reference, commit_oid)
    payload = _pending_mutate(
        root,
        reference,
        _START_MUTATION,
        "addPullRequestReview",
        action="start a review",
        expected_head_oid=commit_oid,
        input={"pullRequestId": pull_request["id"], "commitOID": commit_oid},
    )
    review = payload.get("pullRequestReview") or {}
    review_id = review.get("id")
    if (
        not isinstance(review_id, str)
        or not NODE_ID.fullmatch(review_id)
        or review.get("state") != "PENDING"
        or review.get("viewerDidAuthor") is not True
        or (review.get("commit") or {}).get("oid") != commit_oid
    ):
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.pr_review_start",
        "pr_url": reference.url,
        "review_id": review_id,
        "commit_oid": commit_oid,
    }


_ADD_THREAD_MUTATION = (
    "mutation($input: AddPullRequestReviewThreadInput!) {"
    " addPullRequestReviewThread(input: $input) { thread { "
    + pending_reviews.DRAFT_THREAD_FIELDS
    + " comments(last: 50) { nodes { "
    + pending_reviews.DRAFT_COMMENT_FIELDS
    + " } } } } }"
)
_REVIEW_CHANGED = "Your pending review changed since this page read it. Refresh, then try again."


def _pending_at(
    root: str, reference: PullRequestRef, *, pending_review_id: str, expected_head: str
) -> dict[str, Any]:
    """The PR, at ``expected_head`` and with the viewer's pending review the page saw."""
    if not NODE_ID.fullmatch(pending_review_id):
        raise InvalidPullRequestInput("Expected the pending review the page showed")
    pull_request = pull_request_node(root, reference, pending_reviews.OWN_PENDING_REVIEW)
    if pull_request.get("headRefOid") != expected_head:
        raise PullRequestChangedError()
    own = pending_reviews.own_pending_node(pull_request)
    if own is None:
        raise PullRequestError(pending_reviews.NO_PENDING_REVIEW)
    if own["id"] != pending_review_id:
        raise PullRequestError(_REVIEW_CHANGED)
    return pull_request


def _same_pending_review(
    root: str, reference: PullRequestRef, pending_review_id: str
) -> dict[str, Any]:
    """The viewer's own pending review, refused unless it's ``pending_review_id``."""
    if not NODE_ID.fullmatch(pending_review_id):
        raise InvalidPullRequestInput("Expected the pending review the page showed")
    review = _own_pending_review(root, reference)
    if review["id"] != pending_review_id:
        raise PullRequestError(_REVIEW_CHANGED)
    return review


@_as_one_account
def add_pending_review_comment(
    root: str,
    reference: PullRequestRef,
    *,
    body: str,
    anchor: dict[str, Any],
    pending_review_id: str,
    expected_head: str,
) -> dict[str, Any]:
    """Add a draft conversation on lines to the pending review the page saw, at its head."""
    if anchor["head_sha"] != expected_head:
        raise InvalidPullRequestInput("The lines were picked on another head")
    pull_request = _pending_at(
        root, reference, pending_review_id=pending_review_id, expected_head=expected_head
    )
    if pull_request.get("baseRefOid") != anchor["base_sha"]:
        raise PullRequestChangedError()
    thread_input: dict[str, Any] = {
        "pullRequestReviewId": pending_review_id,
        "path": anchor["path"],
        "line": anchor["line"],
        "side": anchor["side"],
        "subjectType": "LINE",
        "body": body,
    }
    if anchor["start_line"] is not None:
        thread_input.update(startLine=anchor["start_line"], startSide=anchor["side"])
    payload = _pending_mutate(
        root,
        reference,
        _ADD_THREAD_MUTATION,
        "addPullRequestReviewThread",
        action="add this comment",
        input=thread_input,
    )
    thread = pending_reviews.new_draft_thread(payload.get("thread"), pending_review_id)
    if thread is None:
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.pr_review_draft",
        "pr_url": reference.url,
        "review_id": pending_review_id,
        "thread": thread,
    }


_ADD_REPLY_MUTATION = (
    "mutation($input: AddPullRequestReviewThreadReplyInput!) {"
    " addPullRequestReviewThreadReply(input: $input) { comment { "
    + pending_reviews.DRAFT_COMMENT_FIELDS
    + " } } }"
)


@_as_one_account
def add_pending_review_reply(
    root: str,
    reference: PullRequestRef,
    *,
    body: str,
    thread_id: str,
    pending_review_id: str,
    expected_head: str,
) -> dict[str, Any]:
    """Add a draft reply in a conversation on this PR to the pending review the page saw."""
    if not NODE_ID.fullmatch(thread_id):
        raise InvalidPullRequestInput("Invalid review conversation identifier")
    _require_thread_on_pull_request(root, reference, thread_id)
    _pending_at(root, reference, pending_review_id=pending_review_id, expected_head=expected_head)
    payload = _pending_mutate(
        root,
        reference,
        _ADD_REPLY_MUTATION,
        "addPullRequestReviewThreadReply",
        action="add this reply",
        input={
            "pullRequestReviewId": pending_review_id,
            "pullRequestReviewThreadId": thread_id,
            "body": body,
        },
    )
    comment = pending_reviews.new_draft_reply(payload.get("comment"), pending_review_id)
    if comment is None:
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.pr_review_draft",
        "pr_url": reference.url,
        "review_id": pending_review_id,
        "thread_id": thread_id,
        "comment": comment,
    }


# Drafts of the viewer's own pending review, paged, for the checks before a draft change.
_DRAFTS_QUERY = """query($id: ID!, $after: String) {
  node(id: $id) { ... on PullRequestReview {
    comments(first: 100, after: $after) { nodes { id body } pageInfo { hasNextPage endCursor } }
  } }
}"""
# Enough for any review a person writes, and a bound on the reads one change makes.
_MAX_DRAFT_PAGES = 30
_NOT_YOUR_DRAFT = "This draft isn't in your pending review on this pull request."


# Every pending-review path finds the review this one way.
_own_pending_review = pending_reviews.own_pending_review


def _own_draft(
    root: str, reference: PullRequestRef, comment_node_id: str, pending_review_id: str
) -> dict[str, Any]:
    """The draft `comment_node_id`, only in the viewer's own pending review the page saw."""
    if not NODE_ID.fullmatch(comment_node_id):
        raise InvalidPullRequestInput("Invalid draft comment identifier")
    try:
        review = _same_pending_review(root, reference, pending_review_id)
    except InvalidPullRequestInput:
        raise
    except PullRequestError as exc:
        raise PullRequestError(_NOT_YOUR_DRAFT) from exc
    after: str | None = None
    for _ in range(_MAX_DRAFT_PAGES):
        response = _graphql(root, reference, _DRAFTS_QUERY, id=review["id"], after=after)
        node = (response.get("data") or {}).get("node")
        comments = node.get("comments") if isinstance(node, dict) else None
        if not isinstance(comments, dict):
            raise PullRequestError(_NOT_YOUR_DRAFT)
        for draft in comments.get("nodes") or []:
            if isinstance(draft, dict) and draft.get("id") == comment_node_id:
                return draft
        page = comments.get("pageInfo") or {}
        after = page.get("endCursor")
        if not page.get("hasNextPage") or not isinstance(after, str):
            break
    raise PullRequestError(_NOT_YOUR_DRAFT)


_UPDATE_DRAFT_MUTATION = """mutation($input: UpdatePullRequestReviewCommentInput!) {
  updatePullRequestReviewComment(input: $input) { pullRequestReviewComment { id body } }
}"""


@_as_one_account
def update_pending_review_comment(
    root: str,
    reference: PullRequestRef,
    *,
    comment_node_id: str,
    body: str,
    expected_body_sha256: str,
    pending_review_id: str,
) -> dict[str, Any]:
    """Change a draft in the viewer's pending review, only while it reads as last seen."""
    if not SHA256.fullmatch(expected_body_sha256):
        raise InvalidPullRequestInput("Expected the draft's body SHA-256")
    draft = _own_draft(root, reference, comment_node_id, pending_review_id)
    current = draft.get("body") or ""
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_body_sha256:
        raise CommentChangedError()
    payload = _pending_mutate(
        root,
        reference,
        _UPDATE_DRAFT_MUTATION,
        "updatePullRequestReviewComment",
        action="edit this draft",
        input={"pullRequestReviewCommentId": comment_node_id, "body": body},
    )
    edited = payload.get("pullRequestReviewComment") or {}
    if edited.get("id") != comment_node_id or not isinstance(edited.get("body"), str):
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.pr_comment_edit",
        "pr_url": reference.url,
        "comment_id": comment_node_id,
        "body": edited["body"],
    }


_DELETE_DRAFT_MUTATION = """mutation($input: DeletePullRequestReviewCommentInput!) {
  deletePullRequestReviewComment(input: $input) { pullRequestReview { id } }
}"""


@_as_one_account
def delete_pending_review_comment(
    root: str, reference: PullRequestRef, *, comment_node_id: str, pending_review_id: str
) -> dict[str, Any]:
    """Delete a draft from the viewer's pending review on this pull request."""
    _own_draft(root, reference, comment_node_id, pending_review_id)
    payload = _pending_mutate(
        root,
        reference,
        _DELETE_DRAFT_MUTATION,
        "deletePullRequestReviewComment",
        action="delete this draft",
        input={"id": comment_node_id},
    )
    _confirm_review(payload, pending_review_id)
    return {
        "object": "session.github.pr_comment_delete",
        "pr_url": reference.url,
        "comment_id": comment_node_id,
    }


_SUBMIT_PENDING_MUTATION = """mutation($input: SubmitPullRequestReviewInput!) {
  submitPullRequestReview(input: $input) {
    pullRequestReview { id state body createdAt url commit { oid } }
  }
}"""


@_as_one_account
def submit_pending_review(
    root: str,
    reference: PullRequestRef,
    *,
    event: str,
    body: str,
    commit_oid: str,
    pending_review_id: str,
) -> dict[str, Any]:
    """Submit the pending review the page saw with its verdict, once the head is ``commit_oid``."""
    review_id = _same_pending_review(root, reference, pending_review_id)["id"]
    require_head(root, reference, commit_oid)
    payload = _pending_mutate(
        root,
        reference,
        _SUBMIT_PENDING_MUTATION,
        "submitPullRequestReview",
        action="submit this review",
        expected_head_oid=commit_oid,
        input={"pullRequestReviewId": review_id, "event": event, "body": body},
    )
    review = payload.get("pullRequestReview") or {}
    if review.get("id") != review_id or review.get("state") != _SUBMITTED_STATES.get(event):
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.pr_review",
        "pr_url": reference.url,
        "event": event,
        "state": review["state"],
        "review": {
            "id": review_id,
            "state": review["state"],
            "body": review.get("body") if isinstance(review.get("body"), str) else body,
            "created_at": review.get("createdAt"),
            "url": review.get("url"),
            "commit_oid": (review.get("commit") or {}).get("oid") or commit_oid,
        },
    }


_DISCARD_MUTATION = """mutation($input: DeletePullRequestReviewInput!) {
  deletePullRequestReview(input: $input) { pullRequestReview { id } }
}"""


@_as_one_account
def discard_pending_review(
    root: str, reference: PullRequestRef, *, pending_review_id: str
) -> dict[str, Any]:
    """Delete the pending review the page saw, with its draft comments."""
    review_id = _same_pending_review(root, reference, pending_review_id)["id"]
    payload = _pending_mutate(
        root,
        reference,
        _DISCARD_MUTATION,
        "deletePullRequestReview",
        action="discard this review",
        input={"pullRequestReviewId": review_id},
    )
    _confirm_review(payload, review_id)
    return {
        "object": "session.github.pr_review_discard",
        "pr_url": reference.url,
        "review_id": review_id,
    }


_RESOLVE_MUTATIONS = {
    True: (
        "resolveReviewThread",
        "mutation($input: ResolveReviewThreadInput!) {"
        " resolveReviewThread(input: $input) { thread { id isResolved } } }",
    ),
    False: (
        "unresolveReviewThread",
        "mutation($input: UnresolveReviewThreadInput!) {"
        " unresolveReviewThread(input: $input) { thread { id isResolved } } }",
    ),
}


_THREAD_PULL_REQUEST_QUERY = """query($threadId: ID!) {
  node(id: $threadId) { ... on PullRequestReviewThread {
    pullRequest { number repository { nameWithOwner } } } }
}"""
_NOT_ON_PULL_REQUEST = "This review conversation isn't on this pull request."


def _require_thread_on_pull_request(root: str, reference: PullRequestRef, thread_id: str) -> None:
    """Refuse a conversation whose pull request isn't this session's, before any write."""
    response = _graphql(root, reference, _THREAD_PULL_REQUEST_QUERY, threadId=thread_id)
    thread = (response.get("data") or {}).get("node")
    pull_request = thread.get("pullRequest") if isinstance(thread, dict) else None
    if not isinstance(pull_request, dict):
        raise PullRequestError(_NOT_ON_PULL_REQUEST)
    repository = pull_request.get("repository") or {}
    name_with_owner = repository.get("nameWithOwner")
    same_repository = (
        isinstance(name_with_owner, str)
        and name_with_owner.casefold() == reference.repository.casefold()
    )
    if pull_request.get("number") != reference.number or not same_repository:
        raise PullRequestError(_NOT_ON_PULL_REQUEST)


@_as_one_account
def set_thread_resolved(
    root: str, reference: PullRequestRef, *, thread_id: str, resolved: bool
) -> dict[str, Any]:
    """Resolve or reopen a review conversation on this pull request."""
    if not NODE_ID.fullmatch(thread_id):
        raise InvalidPullRequestInput("Invalid review conversation identifier")
    _require_thread_on_pull_request(root, reference, thread_id)
    name, query = _RESOLVE_MUTATIONS[resolved]
    payload = mutate(
        root,
        reference,
        query,
        name,
        action="change this conversation",
        input={"threadId": thread_id},
    )
    thread = payload.get("thread")
    # Without the thread in the state asked for, GitHub hasn't confirmed the change.
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise github_failures.unknown_outcome()
    if thread.get("isResolved") is not resolved:
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.review_thread_resolution",
        "pr_url": reference.url,
        "thread_id": thread_id,
        "resolved": resolved,
    }


def _comment_endpoint(reference: PullRequestRef, kind: str, comment_id: int) -> str:
    if kind not in COMMENT_KINDS:
        raise InvalidPullRequestInput("Expected kind to be issue or review")
    if comment_id < 1:
        raise InvalidPullRequestInput("Expected a comment id")
    # Issue comments and review comments are served from different REST collections.
    collection = "issues/comments" if kind == "issue" else "pulls/comments"
    return f"repos/{reference.repository}/{collection}/{comment_id}"


_NOT_ON_PULL_REQUEST_COMMENT = "This comment isn't on this pull request."


def _comment_on_pull_request(
    comment: dict[str, Any], kind: str, reference: PullRequestRef
) -> bool:
    """Whether the comment's own link names this pull request's repository and number."""
    link = comment.get("issue_url" if kind == "issue" else "pull_request_url")
    if not isinstance(link, str):
        return False
    collection = "issues" if kind == "issue" else "pulls"
    expected = f"/repos/{reference.repository}/{collection}/{reference.number}"
    return urlsplit(link).path.casefold().endswith(expected.casefold())


def _read_comment(
    root: str, reference: PullRequestRef, *, kind: str, comment_id: int
) -> dict[str, Any]:
    """Read a comment, refusing one that isn't on this pull request."""
    endpoint = _comment_endpoint(reference, kind, comment_id)
    rc, output, err = github._gh(
        ["api", *github._host_args(root, reference), endpoint],
        cwd=root,
        token=github._pr_token(root, reference),
    )
    if rc != 0:
        raise github_failures.refusal(err, reference=reference, action="read this comment")
    try:
        comment = json.loads(output)
    except ValueError as exc:
        raise ValueError("GitHub returned an unexpected comment response.") from exc
    if not isinstance(comment, dict):
        raise ValueError("GitHub returned an unexpected comment response.")
    if not _comment_on_pull_request(comment, kind, reference):
        raise PullRequestError(_NOT_ON_PULL_REQUEST_COMMENT)
    return comment


_VIEWER_DID_AUTHOR_QUERY = """query($id: ID!) {
  node(id: $id) { ... on Comment { viewerDidAuthor } }
}"""
_NOT_YOUR_COMMENT = "Only your own comments can be changed here. Change this one on GitHub."


def _host_account_wrote(root: str, reference: PullRequestRef, comment: dict[str, Any]) -> bool:
    """Whether the host's account wrote ``comment``; False where GitHub doesn't say."""
    node_id = comment.get("node_id")
    if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
        return False
    response = _graphql(root, reference, _VIEWER_DID_AUTHOR_QUERY, id=node_id)
    node = (response.get("data") or {}).get("node")
    return isinstance(node, dict) and node.get("viewerDidAuthor") is True


def _own_comment(
    root: str, reference: PullRequestRef, *, kind: str, comment_id: int
) -> dict[str, Any]:
    """A comment on this pull request the host's account wrote, or a refusal before any write.

    GitHub lets a repository admin change anyone's comment, so authorship is checked here.
    """
    comment = _read_comment(root, reference, kind=kind, comment_id=comment_id)
    if not _host_account_wrote(root, reference, comment):
        raise PullRequestError(_NOT_YOUR_COMMENT)
    return comment


def _rest_write(
    root: str,
    reference: PullRequestRef,
    *,
    method: str,
    endpoint: str,
    fields: dict[str, Any] | None,
    action: str,
) -> str:
    """Send one REST write and return its output; a failure after the send is classified."""
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        host = github._host_args(root, reference)
        token = github._write_token(root, reference, timed_out=AUTHENTICATION_TIMED_OUT)
        if fields is None:
            rc, output, err = github._gh(
                ["api", *host, "-X", method, endpoint], cwd=root, token=token
            )
        else:
            rc, output, err = github._pr_json_write(
                root, host=host, token=token, method=method, endpoint=endpoint, fields=fields
            )
        if github._gh_timed_out.get() or rc is None:
            raise github_failures.unknown_outcome()
    if rc != 0:
        raise github_failures.write_failure(err, reference=reference, action=action)
    return output


@_as_one_account
def edit_comment(
    root: str,
    reference: PullRequestRef,
    *,
    kind: str,
    comment_id: int,
    body: str,
    expected_body_sha256: str,
) -> dict[str, Any]:
    """Change your own comment's text, only while it still reads as the client last saw it."""
    endpoint = _comment_endpoint(reference, kind, comment_id)
    if not SHA256.fullmatch(expected_body_sha256):
        raise InvalidPullRequestInput("Expected the comment's body SHA-256")
    current = _own_comment(root, reference, kind=kind, comment_id=comment_id)
    current_body = current.get("body") or ""
    if hashlib.sha256(current_body.encode("utf-8")).hexdigest() != expected_body_sha256:
        raise CommentChangedError()
    output = _rest_write(
        root,
        reference,
        method="PATCH",
        endpoint=endpoint,
        fields={"body": body},
        action="edit this comment",
    )
    try:
        edited = json.loads(output)
    except ValueError as exc:
        raise github_failures.unknown_outcome() from exc
    # Some gh wrappers exit 0 on an HTTP error; only the edited comment confirms the edit.
    if not isinstance(edited, dict) or edited.get("id") != comment_id:
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.comment_edit",
        "pr_url": reference.url,
        "comment_id": comment_id,
        "body": edited.get("body") or "",
    }


@_as_one_account
def delete_comment(
    root: str, reference: PullRequestRef, *, kind: str, comment_id: int
) -> dict[str, Any]:
    """Delete your own comment on this pull request."""
    endpoint = _comment_endpoint(reference, kind, comment_id)
    _own_comment(root, reference, kind=kind, comment_id=comment_id)
    output = _rest_write(
        root,
        reference,
        method="DELETE",
        endpoint=endpoint,
        fields=None,
        action="delete this comment",
    )
    # A delete answers with no body; an error body on exit 0 doesn't confirm it.
    if output.strip():
        raise github_failures.unknown_outcome()
    return {
        "object": "session.github.comment_deleted",
        "pr_url": reference.url,
        "comment_id": comment_id,
    }


# The comment's reactions after the change, so the page needn't read them again.
_REACTION_SUBJECT = (
    "subject { ... on Reactable { viewerCanReact"
    " reactionGroups { content viewerHasReacted reactors { totalCount } } } }"
)
_REACTION_MUTATIONS = {
    "add": (
        "addReaction",
        "mutation($input: AddReactionInput!) {"
        f" addReaction(input: $input) {{ reaction {{ content }} {_REACTION_SUBJECT} }} }}",
    ),
    "remove": (
        "removeReaction",
        "mutation($input: RemoveReactionInput!) {"
        f" removeReaction(input: $input) {{ reaction {{ content }} {_REACTION_SUBJECT} }} }}",
    ),
}


def _set_reaction(
    root: str,
    reference: PullRequestRef,
    *,
    verb: str,
    kind: str,
    comment_id: int,
    content: str,
) -> dict[str, Any]:
    if content not in REACTION_CONTENTS:
        raise InvalidPullRequestInput("Expected a GitHub reaction, such as THUMBS_UP")
    node_id = _read_comment(root, reference, kind=kind, comment_id=comment_id).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("GitHub returned an unexpected comment response.")
    name, query = _REACTION_MUTATIONS[verb]
    payload = mutate(
        root,
        reference,
        query,
        name,
        action="change this reaction",
        input={"subjectId": node_id, "content": content},
    )
    _confirm_reaction(payload, content=content, reacted=verb == "add")
    return {
        "object": "session.github.reaction",
        "pr_url": reference.url,
        "comment_id": comment_id,
        "content": content,
        "reacted": verb == "add",
        **_reactions_after(payload),
    }


def _confirm_reaction(payload: dict[str, Any], *, content: str, reacted: bool) -> None:
    """Report success only when GitHub's answer confirms the reaction change.

    Either the changed reaction or the comment's group for it must confirm it; neither may
    contradict it.
    """
    confirmed = False
    reaction = payload.get("reaction")
    if reaction is not None:
        if not isinstance(reaction, dict) or reaction.get("content") != content:
            raise github_failures.unknown_outcome()
        confirmed = True
    subject = payload.get("subject")
    groups = subject.get("reactionGroups") if isinstance(subject, dict) else None
    for group in groups if isinstance(groups, list) else []:
        if (
            isinstance(group, dict)
            and group.get("content") == content
            and isinstance(group.get("viewerHasReacted"), bool)
        ):
            if group["viewerHasReacted"] is not reacted:
                raise github_failures.unknown_outcome()
            confirmed = True
    if not confirmed:
        raise github_failures.unknown_outcome()


def _reactions_after(payload: dict[str, Any]) -> dict[str, Any]:
    """The comment's reactions as GitHub answered the change; none where it didn't say."""
    subject = payload.get("subject")
    if not isinstance(subject, dict) or "reactionGroups" not in subject:
        return {}
    return {
        "reactions": reactions(subject.get("reactionGroups")),
        "can_react": bool(subject.get("viewerCanReact")),
    }


@_as_one_account
def add_reaction(root: str, reference: PullRequestRef, **fields: Any) -> dict[str, Any]:
    """Add the session user's reaction to a comment."""
    return _set_reaction(root, reference, verb="add", **fields)


@_as_one_account
def remove_reaction(root: str, reference: PullRequestRef, **fields: Any) -> dict[str, Any]:
    """Remove the session user's reaction from a comment."""
    return _set_reaction(root, reference, verb="remove", **fields)
