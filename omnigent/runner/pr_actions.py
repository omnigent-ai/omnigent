"""Review, comment, merge queue, stack and image calls on the session's selected pull request.

These are the wrappers the workspace API's review, merge queue, stack and image methods
call. Each checks its fields and the session's link to a GitHub pull request, then runs
the GitHub code in :mod:`omnigent.runner.github_reviews`,
:mod:`omnigent.runner.github_merge_queue`, :mod:`omnigent.runner.github_stack` or
:mod:`omnigent.runner.github_images`.
"""

from __future__ import annotations

from typing import Any

from omnigent.runner import (
    github_images,
    github_lifecycle,
    github_merge_queue,
    github_reviews,
    github_stack,
    pr_resource,
)
from omnigent.runner.github_images import PullRequestImage
from omnigent.runner.github_review_fields import (
    COMMENT_KINDS,
    COMMENT_REVIEW_EVENT,
    NODE_ID,
    PENDING_EVENTS,
    SHA256,
    SIDES,
    VERDICT_STATES,
)
from omnigent.runner.pull_request_errors import InvalidPullRequestInput
from omnigent.runner.session_prs import PullRequestRef

_UNSELECTED = "Select a pull request to change it"
AUTO_MERGE_BATCH_LIMIT = 20


def _target(session_id: str | None, pr_url: str | None) -> PullRequestRef:
    """The session's selected pull request, which must be on GitHub."""
    if not session_id or not pr_url:
        raise ValueError(_UNSELECTED)
    reference = pr_resource._selected_pr(session_id, pr_url)
    if reference.provider != "github":
        raise ValueError("This action is supported only for GitHub pull requests")
    return reference


def _commit(value: object, field: str) -> str:
    try:
        return pr_resource._commit_sha(value, field)
    except ValueError as exc:
        raise InvalidPullRequestInput(str(exc)) from exc


def _body(value: object) -> str:
    try:
        return pr_resource._comment_body(value)
    except ValueError as exc:
        raise InvalidPullRequestInput(str(exc)) from exc


def _node_id(value: object, message: str) -> str:
    if not isinstance(value, str) or not NODE_ID.fullmatch(value):
        raise InvalidPullRequestInput(message)
    return value


def _pending_review_id(value: object) -> str:
    """The pending review the page showed, which every change to it names."""
    return _node_id(value, "Expected the pending review the page showed")


def _event(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InvalidPullRequestInput(f"Expected event to be one of {', '.join(sorted(allowed))}")
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise InvalidPullRequestInput("Expected the text's SHA-256")
    return value


def submit_review(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    event: object,
    body: object,
    commit_oid: object,
) -> dict[str, Any]:
    """Submit a comment-only review. Approvals and change requests go through the verdict."""
    if event != COMMENT_REVIEW_EVENT:
        raise InvalidPullRequestInput("Use a review verdict to approve or request changes")
    text = _body(body)
    commit = _commit(commit_oid, "commit_oid")
    return github_reviews.submit_review(
        root, _target(session_id, pr_url), event=COMMENT_REVIEW_EVENT, body=text, commit_oid=commit
    )


def submit_review_verdict(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    event: object,
    body: object,
    commit_oid: object,
) -> dict[str, Any]:
    """Approve or request changes, on the head the user saw."""
    verdict = _event(event, frozenset(VERDICT_STATES))
    text = _body(body) if body not in ("", None) else ""
    commit = _commit(commit_oid, "commit_oid")
    return github_reviews.submit_review(
        root, _target(session_id, pr_url), event=verdict, body=text, commit_oid=commit
    )


def get_pending_review(root: str, *, session_id: str | None, pr_url: str | None) -> dict[str, Any]:
    """The viewer's pending review and its draft comments."""
    return github_reviews.get_pending_review(root, _target(session_id, pr_url))


def start_pending_review(
    root: str, *, session_id: str | None, pr_url: str | None, commit_oid: object
) -> dict[str, Any]:
    """Start a pending review on the head the user saw."""
    commit = _commit(commit_oid, "commit_oid")
    return github_reviews.start_pending_review(
        root, _target(session_id, pr_url), commit_oid=commit
    )


def _anchor(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidPullRequestInput("Select lines in the diff before commenting")
    path, side, line = value.get("path"), value.get("side"), value.get("line")
    start = value.get("start_line")
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise InvalidPullRequestInput("Expected a repository-relative file path")
    if side not in SIDES:
        raise InvalidPullRequestInput("Select lines on one side of the diff")
    if type(line) is not int or line < 1:
        raise InvalidPullRequestInput("Expected a positive line")
    if start is not None and (type(start) is not int or not 1 <= start < line):
        raise InvalidPullRequestInput("Expected a positive line or increasing line range")
    return {
        "path": path,
        "side": side,
        "line": line,
        "start_line": start,
        "head_sha": _commit(value.get("head_sha"), "head_sha"),
        "base_sha": _commit(value.get("base_sha"), "base_sha"),
    }


def add_pending_review_comment(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    body: object,
    anchor: object,
    pending_review_id: object,
    expected_head: object,
    in_reply_to_thread_id: object = None,
) -> dict[str, Any]:
    """Add a draft on lines, or a draft reply in a conversation, to the pending review seen."""
    text = _body(body)
    review_id = _pending_review_id(pending_review_id)
    head = _commit(expected_head, "expected_head_oid")
    if in_reply_to_thread_id not in (None, ""):
        if anchor is not None:
            raise InvalidPullRequestInput("A reply goes in its conversation, without lines")
        thread_id = _node_id(in_reply_to_thread_id, "Expected a review conversation")
        return github_reviews.add_pending_review_reply(
            root,
            _target(session_id, pr_url),
            body=text,
            thread_id=thread_id,
            pending_review_id=review_id,
            expected_head=head,
        )
    checked = _anchor(anchor)
    return github_reviews.add_pending_review_comment(
        root,
        _target(session_id, pr_url),
        body=text,
        anchor=checked,
        pending_review_id=review_id,
        expected_head=head,
    )


def update_pending_review_comment(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    comment_node_id: object,
    body: object,
    expected_body_sha256: object,
    pending_review_id: object,
) -> dict[str, Any]:
    """Change a draft in the viewer's pending review, only while it reads as the client saw it."""
    draft_id = _node_id(comment_node_id, "Expected a draft comment")
    text = _body(body)
    return github_reviews.update_pending_review_comment(
        root,
        _target(session_id, pr_url),
        comment_node_id=draft_id,
        body=text,
        expected_body_sha256=_sha256(expected_body_sha256),
        pending_review_id=_pending_review_id(pending_review_id),
    )


def delete_pending_review_comment(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    comment_node_id: object,
    pending_review_id: object,
) -> dict[str, Any]:
    """Delete a draft from the viewer's pending review."""
    draft_id = _node_id(comment_node_id, "Expected a draft comment")
    return github_reviews.delete_pending_review_comment(
        root,
        _target(session_id, pr_url),
        comment_node_id=draft_id,
        pending_review_id=_pending_review_id(pending_review_id),
    )


def submit_pending_review(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    event: object,
    body: object,
    commit_oid: object,
    pending_review_id: object,
) -> dict[str, Any]:
    """Submit the pending review the user saw with its verdict, on the head they saw."""
    verdict = _event(event, PENDING_EVENTS)
    text = _body(body) if body not in ("", None) else ""
    commit = _commit(commit_oid, "commit_oid")
    return github_reviews.submit_pending_review(
        root,
        _target(session_id, pr_url),
        event=verdict,
        body=text,
        commit_oid=commit,
        pending_review_id=_pending_review_id(pending_review_id),
    )


def discard_pending_review(
    root: str, *, session_id: str | None, pr_url: str | None, pending_review_id: object
) -> dict[str, Any]:
    """Delete the pending review the user saw, with its draft comments."""
    return github_reviews.discard_pending_review(
        root,
        _target(session_id, pr_url),
        pending_review_id=_pending_review_id(pending_review_id),
    )


def set_review_thread_resolved(
    root: str, *, session_id: str | None, pr_url: str | None, thread_id: object, resolved: object
) -> dict[str, Any]:
    """Resolve or reopen a review conversation."""
    checked = _node_id(thread_id, "Expected a review conversation")
    if not isinstance(resolved, bool):
        raise InvalidPullRequestInput("Expected resolved to be true or false")
    return github_reviews.set_thread_resolved(
        root, _target(session_id, pr_url), thread_id=checked, resolved=resolved
    )


def _comment_fields(kind: object, comment_id: object) -> dict[str, Any]:
    if kind not in COMMENT_KINDS:
        raise InvalidPullRequestInput("Expected kind to be issue or review")
    if type(comment_id) is not int or comment_id < 1:
        raise InvalidPullRequestInput("Expected a comment id")
    return {"kind": kind, "comment_id": comment_id}


def edit_comment(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    kind: object,
    comment_id: object,
    body: object,
    expected_body_sha256: object,
) -> dict[str, Any]:
    """Change your own comment's text, only while it still reads as the client last saw it."""
    fields = _comment_fields(kind, comment_id)
    text = _body(body)
    return github_reviews.edit_comment(
        root,
        _target(session_id, pr_url),
        body=text,
        expected_body_sha256=_sha256(expected_body_sha256),
        **fields,
    )


def delete_comment(
    root: str, *, session_id: str | None, pr_url: str | None, kind: object, comment_id: object
) -> dict[str, Any]:
    """Delete your own comment."""
    fields = _comment_fields(kind, comment_id)
    return github_reviews.delete_comment(root, _target(session_id, pr_url), **fields)


def add_reaction(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    kind: object,
    comment_id: object,
    content: object,
) -> dict[str, Any]:
    """Add the host account's reaction to a comment."""
    fields = _comment_fields(kind, comment_id)
    return github_reviews.add_reaction(
        root, _target(session_id, pr_url), content=str(content), **fields
    )


def remove_reaction(
    root: str,
    *,
    session_id: str | None,
    pr_url: str | None,
    kind: object,
    comment_id: object,
    content: object,
) -> dict[str, Any]:
    """Remove the host account's reaction from a comment."""
    fields = _comment_fields(kind, comment_id)
    return github_reviews.remove_reaction(
        root, _target(session_id, pr_url), content=str(content), **fields
    )


def get_merge_queue(root: str, *, session_id: str | None, pr_url: str | None) -> dict[str, Any]:
    """Whether the pull request's branch has a merge queue, and its place in it."""
    return github_merge_queue.get_merge_queue(root, _target(session_id, pr_url))


def enqueue_pull_request(
    root: str, *, session_id: str | None, pr_url: str | None, expected_head_oid: object
) -> dict[str, Any]:
    """Add the pull request to its base branch's merge queue, at the head the user saw."""
    head = _commit(expected_head_oid, "expected_head_oid")
    return github_merge_queue.enqueue(root, _target(session_id, pr_url), expected_head_oid=head)


def dequeue_pull_request(
    root: str, *, session_id: str | None, pr_url: str | None
) -> dict[str, Any]:
    """Remove the pull request from its merge queue."""
    return github_merge_queue.dequeue(root, _target(session_id, pr_url))


def get_pull_request_stack(
    root: str, *, session_id: str | None, pr_url: str | None
) -> dict[str, Any]:
    """The stack around the pull request, found through its branches."""
    return github_stack.pull_request_stack(root, _target(session_id, pr_url))


def set_pull_request_state(
    root: str, *, session_id: str | None, pr_url: str | None, state: object
) -> dict[str, Any]:
    """Close or reopen the pull request."""
    if state not in ("closed", "open"):
        raise InvalidPullRequestInput("Expected state to be closed or open")
    return github_lifecycle.set_state(root, _target(session_id, pr_url), state=str(state))


def set_pull_request_draft(
    root: str, *, session_id: str | None, pr_url: str | None, draft: object
) -> dict[str, Any]:
    """Make the pull request a draft, or mark it ready for review."""
    if not isinstance(draft, bool):
        raise InvalidPullRequestInput("Expected draft to be true or false")
    return github_lifecycle.set_draft(root, _target(session_id, pr_url), draft=draft)


def enable_auto_merge(
    root: str, *, session_id: str | None, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Turn on auto-merge for each pull request, in one batch.

    Every pull request is checked against the session before any is sent.
    """
    if not 1 <= len(items) <= AUTO_MERGE_BATCH_LIMIT:
        raise InvalidPullRequestInput(
            f"Enable auto-merge for 1 to {AUTO_MERGE_BATCH_LIMIT} pull requests at once"
        )
    batch: list[github_lifecycle.AutoMergeItem] = []
    for item in items:
        method = item.get("merge_method")
        if method not in github_lifecycle.MERGE_METHODS:
            raise InvalidPullRequestInput("Expected merge_method to be merge, squash, or rebase")
        reference = _target(session_id, item.get("pr_url"))
        if any(queued.reference.url == reference.url for queued in batch):
            raise InvalidPullRequestInput("Name each pull request once")
        batch.append(
            github_lifecycle.AutoMergeItem(
                reference=reference,
                merge_method=str(method),
                expected_head_oid=_commit(item.get("expected_head_oid"), "expected_head_oid"),
            )
        )
    return github_lifecycle.enable_auto_merge(root, batch)


def read_pull_request_image(
    root: str, *, session_id: str | None, pr_url: str | None, image_url: str
) -> PullRequestImage:
    """The attachment image at ``image_url`` on the selected pull request's host."""
    return github_images.read_pull_request_image(root, _target(session_id, pr_url), image_url)
