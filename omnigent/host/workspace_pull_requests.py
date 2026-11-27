"""The workspace API's pull request methods, as the host serves them over the tunnel.

Each method runs the same code as the host's ``github_*`` tunnel ops, so it answers
with the REST route's payload. The server decides which pull requests belong to a
session: with each call it sends the session and its vouch for the pull request in
the call frame, never in the request message. A call without them isn't served.
Which caller may change a pull request is the server's gate; the host serves the
writes only for a call the server sent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host.frames import PullRequestVouch
from omnigent.host.workspace_pull_request_input import PullRequestRefused
from omnigent.runner import github_lookup, github_search, pr_actions, pr_resource
from omnigent.runner.github_images import ImageRefused, PullRequestImage
from omnigent.runner.pull_request_errors import (
    InvalidPullRequestInput,
    PullRequestError,
    UnknownOutcomeError,
)
from omnigent.workspace_fs import WorkspaceReader


@dataclass(frozen=True)
class PullRequestCall:
    """What the server sent with a pull request call, from its call frame.

    :param session_id: The session the caller was authorized for, e.g. ``"conv_1"``.
    :param vouch: The server's vouch for the call's PR, or ``None`` when the server
        stores none of the session's PRs yet, so this host's own record decides.
    :param account_rights: Whether the server asked for the host account's rights in a
        review read, which it does only for the session's owner.
    """

    session_id: str
    vouch: PullRequestVouch | None = None
    account_rights: bool = False


# One pull request method: the reader, the call and the request, to the REST payload.
PullRequestOp = Callable[[WorkspaceReader, PullRequestCall, Any], dict[str, Any]]
# One streamed pull request method: the reader, the call and the request, to the bytes.
PullRequestStreamOp = Callable[[WorkspaceReader, PullRequestCall, Any], PullRequestImage]
_Result = TypeVar("_Result")


def _url(request: Any) -> str | None:
    return request.pr_url or None


def _optional(value: str) -> str | None:
    return value or None


def _diff(
    reader: WorkspaceReader, call: PullRequestCall, request: pb.GetPullRequestDiffRequest
) -> dict[str, Any]:
    pins = {"head_sha": _optional(request.head_sha), "base_sha": _optional(request.base_sha)}
    if not request.path:
        return reader.github_pr_diff(call.session_id, _url(request), **pins)
    if request.path.startswith("/"):
        raise ConnectError(Code.INVALID_ARGUMENT, "path must be relative to the repository")
    return reader.github_file_diff(
        _optional(request.base),
        request.path,
        session_id=call.session_id,
        pr_url=_url(request),
        previous_path=_optional(request.previous_path),
        **pins,
    )


def _root(reader: WorkspaceReader) -> str:
    return str(reader.root)


def _anchor(
    request: pb.CreateReviewThreadRequest | pb.AddPendingReviewCommentRequest,
) -> dict[str, Any] | None:
    if not request.HasField("anchor"):
        return None
    anchor = request.anchor
    return {
        "path": anchor.path,
        "side": anchor.side,
        "line": anchor.line,
        "start_line": anchor.start_line if anchor.HasField("start_line") else None,
        "head_sha": anchor.head_sha,
        "base_sha": anchor.base_sha,
    }


def _reviewers(reviewers: Any) -> list[dict[str, str]]:
    return [{"login": reviewer.login, "kind": reviewer.kind} for reviewer in reviewers]


def _details(
    reader: WorkspaceReader, call: PullRequestCall, request: pb.UpdatePullRequestDetailsRequest
) -> dict[str, Any]:
    return pr_resource.update_pr_details(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        title=request.title if request.HasField("title") else None,
        body=request.body if request.HasField("body") else None,
    )


def _set_account(
    reader: WorkspaceReader, call: PullRequestCall, request: pb.SetPullRequestAccountRequest
) -> dict[str, Any]:
    # refuse_unserved_request has refused a call without a PR, or with a remote. An empty
    # account keeps the current one; set_pr_preference reads "" as clearing it.
    return pr_resource.set_pr_preference(
        _root(reader),
        account=request.account or None,
        remote=None,
        session_id=call.session_id,
        pr_url=request.pr_url,
    )


def _merge_as(
    reader: WorkspaceReader, call: PullRequestCall, request: pb.MergePullRequestAsAccountRequest
) -> dict[str, Any]:
    # refuse_unserved_request has refused an empty account, so it's never passed as None.
    return pr_resource.merge_pr_as(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        merge_method=_optional(request.merge_method),
        expected_head=_optional(request.expected_head_oid),
        account=request.account,
    )


def _named(action: Callable[..., dict[str, Any]], *fields: str) -> PullRequestOp:
    """An op that passes the session, the PR and the request's ``fields`` to ``action``."""

    def run(reader: WorkspaceReader, call: PullRequestCall, request: Any) -> dict[str, Any]:
        return action(
            _root(reader),
            session_id=call.session_id,
            pr_url=_url(request),
            **{field: getattr(request, field) for field in fields},
        )

    return run


def _enable_auto_merge(
    reader: WorkspaceReader, call: PullRequestCall, request: pb.EnableAutoMergeRequest
) -> dict[str, Any]:
    return pr_actions.enable_auto_merge(
        _root(reader),
        session_id=call.session_id,
        items=[
            {
                "pr_url": item.pr_url,
                "merge_method": item.merge_method,
                "expected_head_oid": item.expected_head_oid,
            }
            for item in request.pull_requests
        ],
    )


# The session owner's review and comment changes, and the pending review they work on.
_REVIEW_OPS: dict[str, PullRequestOp] = {
    "GetPendingReview": _named(pr_actions.get_pending_review),
    "StartPendingReview": _named(pr_actions.start_pending_review, "commit_oid"),
    "AddPendingReviewComment": lambda reader, call, request: pr_actions.add_pending_review_comment(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        body=request.body,
        anchor=_anchor(request),
        pending_review_id=request.pending_review_id,
        expected_head=request.expected_head_oid,
        in_reply_to_thread_id=request.in_reply_to_thread_id,
    ),
    "UpdatePendingReviewComment": _named(
        pr_actions.update_pending_review_comment,
        "comment_node_id",
        "body",
        "expected_body_sha256",
        "pending_review_id",
    ),
    "DeletePendingReviewComment": _named(
        pr_actions.delete_pending_review_comment, "comment_node_id", "pending_review_id"
    ),
    "SubmitPendingReview": _named(
        pr_actions.submit_pending_review, "event", "body", "commit_oid", "pending_review_id"
    ),
    "DiscardPendingReview": _named(pr_actions.discard_pending_review, "pending_review_id"),
    "SubmitReview": _named(pr_actions.submit_review, "event", "body", "commit_oid"),
    "SubmitReviewVerdict": _named(pr_actions.submit_review_verdict, "event", "body", "commit_oid"),
    "SetReviewThreadResolved": _named(
        pr_actions.set_review_thread_resolved, "thread_id", "resolved"
    ),
    "EditComment": _named(
        pr_actions.edit_comment, "kind", "comment_id", "body", "expected_body_sha256"
    ),
    "DeleteComment": _named(pr_actions.delete_comment, "kind", "comment_id"),
    "AddReaction": _named(pr_actions.add_reaction, "kind", "comment_id", "content"),
    "RemoveReaction": _named(pr_actions.remove_reaction, "kind", "comment_id", "content"),
}

# The session owner's merge queue: where the pull request stands, and adding or removing it.
_MERGE_QUEUE_OPS: dict[str, PullRequestOp] = {
    "GetMergeQueue": _named(pr_actions.get_merge_queue),
    "EnqueuePullRequest": _named(pr_actions.enqueue_pull_request, "expected_head_oid"),
    "DequeuePullRequest": _named(pr_actions.dequeue_pull_request),
}


PULL_REQUEST_OPS: dict[str, PullRequestOp] = {
    "GetPullRequest": lambda reader, call, request: reader.github_info(
        call.session_id, _url(request)
    ),
    "ListPullRequestFiles": lambda reader, call, request: reader.github_changes(
        call.session_id,
        _url(request),
        head_sha=_optional(request.head_sha),
        base_sha=_optional(request.base_sha),
    ),
    "GetPullRequestDiff": _diff,
    "ListPullRequestCommits": lambda reader, call, request: reader.github_commits(
        call.session_id, _url(request), head_sha=_optional(request.head_sha)
    ),
    "GetCommitDiff": lambda reader, call, request: reader.github_commit_diff(
        call.session_id, _url(request), commit_sha=_optional(request.commit_sha)
    ),
    "ComparePullRequestRange": lambda reader, call, request: reader.github_compare_diff(
        call.session_id,
        _url(request),
        head_sha=_optional(request.head_sha),
        from_sha=_optional(request.from_sha),
    ),
    "ListReviewThreads": lambda reader, call, request: reader.github_review_threads(
        call.session_id,
        request.pr_url,
        request.page_token,
        request.head_sha,
        request.base_sha,
        account_rights=call.account_rights,
    ),
    "ListReviewThreadComments": lambda reader, call, request: reader.github_review_thread_comments(
        call.session_id,
        request.pr_url,
        request.thread_id,
        request.page_token,
        account_rights=call.account_rights,
    ),
    # The session owner's: what the edit controls offer, and the changes themselves.
    "ListLabels": lambda reader, call, request: reader.github_labels(
        call.session_id, _url(request), request.text
    ),
    "ListReviewerCandidates": lambda reader, call, request: reader.github_reviewers(
        call.session_id, _url(request)
    ),
    "SearchReviewers": lambda reader, call, request: github_search.search_reviewers(
        _root(reader),
        pr_actions._target(call.session_id, _url(request)),
        text=request.text,
        page_size=request.page_size,
    ),
    "SearchCompletions": lambda reader, call, request: github_search.search_completions(
        _root(reader),
        pr_actions._target(call.session_id, _url(request)),
        kind=pb.CompletionKind.Name(request.kind).removeprefix("COMPLETION_KIND_"),
        text=request.text,
    ),
    "GetMergeOptions": lambda reader, call, request: reader.github_merge_options(
        call.session_id, _url(request)
    ),
    "CreateComment": lambda reader, call, request: pr_resource.create_pr_comment(
        _root(reader), session_id=call.session_id, pr_url=_url(request), body=request.body
    ),
    "CreateReviewThread": lambda reader, call, request: pr_resource.create_pr_review_thread(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        body=request.body,
        anchor=_anchor(request),
    ),
    "ReplyToReviewThread": lambda reader, call, request: pr_resource.reply_to_pr_review_thread(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        thread_id=request.thread_id,
        body=request.body,
    ),
    "UpdatePullRequestDetails": _details,
    "UpdateLabels": lambda reader, call, request: pr_resource.update_pr_labels(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        add=list(request.add),
        remove=list(request.remove),
    ),
    "UpdateReviewers": lambda reader, call, request: pr_resource.update_pr_reviewers(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        add=_reviewers(request.add),
        remove=_reviewers(request.remove),
    ),
    "SetPullRequestAccount": _set_account,
    "MergePullRequest": lambda reader, call, request: pr_resource.merge_pr(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        merge_method=_optional(request.merge_method),
        expected_head=_optional(request.expected_head_oid),
    ),
    "MergePullRequestAsAccount": _merge_as,
    "DisableAutoMerge": lambda reader, call, request: pr_resource.disable_pr_auto_merge(
        _root(reader), session_id=call.session_id, pr_url=_url(request)
    ),
    "SetPullRequestState": _named(pr_actions.set_pull_request_state, "state"),
    "SetPullRequestDraft": _named(pr_actions.set_pull_request_draft, "draft"),
    "EnableAutoMerge": _enable_auto_merge,
    **_REVIEW_OPS,
    **_MERGE_QUEUE_OPS,
    # The stack read lists the repository's other pull requests as the host's account.
    "GetPullRequestStack": _named(pr_actions.get_pull_request_stack),
    # Read the URLs the request names, never the session's or the vouch's pull requests.
    "LookupPullRequest": lambda reader, _call, request: github_lookup.lookup_pull_request(
        _root(reader), pr_url=request.pr_url
    ),
    "GetPullRequestListStats": lambda reader, _call, request: (
        github_lookup.pull_request_list_stats(_root(reader), pr_urls=list(request.pr_urls))
    ),
    # Finds pull requests from the request alone, never from the session's or the vouch.
    "ListBranchPullRequests": lambda reader, _call, request: github_search.branch_pull_requests(
        _root(reader), branch=request.branch
    ),
    # The runner takes the media type from the bytes, as on the REST route.
    "UploadAttachment": lambda reader, call, request: pr_resource.upload_pr_attachment(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        name=request.name,
        content=request.content,
    ),
}


# The owner's: the images a pull request links to, which GitHub serves only to its viewers.
PULL_REQUEST_STREAM_OPS: dict[str, PullRequestStreamOp] = {
    "ReadPullRequestImage": lambda reader, call, request: pr_actions.read_pull_request_image(
        _root(reader),
        session_id=call.session_id,
        pr_url=_url(request),
        image_url=request.image_url,
    ),
}
# Every pull request method this host serves, unary or streamed.
PULL_REQUEST_METHODS: frozenset[str] = frozenset(PULL_REQUEST_OPS) | frozenset(
    PULL_REQUEST_STREAM_OPS
)


def run_pull_request_op(
    name: str, reader: WorkspaceReader, call: PullRequestCall, request: Any
) -> dict[str, Any]:
    """Run one pull request method under the server's vouch, as the tunnel op would.

    Blocking: call it on the worker thread, since the vouch is a context variable that
    a new thread doesn't inherit.

    :raises PullRequestRefused: FAILED_PRECONDITION for a PR the session doesn't link, or
        another refusal with its code and reason; UNKNOWN for a change GitHub never confirmed.
    """
    return _under_vouch(call, lambda: PULL_REQUEST_OPS[name](reader, call, request))


def run_pull_request_stream_op(
    name: str, reader: WorkspaceReader, call: PullRequestCall, request: Any
) -> PullRequestImage:
    """Run one streamed pull request method under the server's vouch; blocking, as above.

    :raises PullRequestRefused: As :func:`run_pull_request_op`; UNAVAILABLE for an image
        GitHub didn't send, which a client may retry.
    """
    return _under_vouch(call, lambda: PULL_REQUEST_STREAM_OPS[name](reader, call, request))


def _under_vouch(call: PullRequestCall, operation: Callable[[], _Result]) -> _Result:
    vouch = call.vouch or PullRequestVouch()
    with pr_resource.server_tracking(vouch.server_tracked is True, vouch.default_pr_url):
        try:
            return operation()
        except ValueError as exc:
            raise _refusal(exc) from exc


def _refusal(exc: ValueError) -> PullRequestRefused:
    """The Connect error for a failed operation, with the reason a client reads."""
    reason = exc.reason if isinstance(exc, PullRequestError) else None
    if isinstance(exc, ImageRefused) and exc.reason == "fetch_failed":
        return PullRequestRefused(Code.UNAVAILABLE, str(exc), reason)
    if isinstance(exc, UnknownOutcomeError):
        # The change may have been made: never a refusal a client would retry past.
        return PullRequestRefused(Code.UNKNOWN, str(exc), None)
    if isinstance(exc, InvalidPullRequestInput):
        return PullRequestRefused(Code.INVALID_ARGUMENT, str(exc), reason)
    return PullRequestRefused(Code.FAILED_PRECONDITION, str(exc), reason)
