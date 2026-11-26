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
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host.frames import PullRequestVouch
from omnigent.runner import pr_resource
from omnigent.workspace_fs import WorkspaceReader


@dataclass(frozen=True)
class PullRequestCall:
    """What the server sent with a pull request call, from its call frame.

    :param session_id: The session the caller was authorized for, e.g. ``"conv_1"``.
    :param vouch: The server's vouch for the call's PR, or ``None`` when the server
        stores none of the session's PRs yet, so this host's own record decides.
    """

    session_id: str
    vouch: PullRequestVouch | None = None


# One pull request method: the reader, the call and the request, to the REST payload.
PullRequestOp = Callable[[WorkspaceReader, PullRequestCall, Any], dict[str, Any]]


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


def _anchor(request: pb.CreateReviewThreadRequest) -> dict[str, Any] | None:
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
        call.session_id, request.pr_url, request.page_token, request.head_sha, request.base_sha
    ),
    "ListReviewThreadComments": lambda reader, call, request: reader.github_review_thread_comments(
        call.session_id, request.pr_url, request.thread_id, request.page_token
    ),
    # The session owner's: what the edit controls offer, and the changes themselves.
    "ListLabels": lambda reader, call, request: reader.github_labels(
        call.session_id, _url(request)
    ),
    "ListReviewerCandidates": lambda reader, call, request: reader.github_reviewers(
        call.session_id, _url(request)
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
}


def run_pull_request_op(
    name: str, reader: WorkspaceReader, call: PullRequestCall, request: Any
) -> dict[str, Any]:
    """Run one pull request method under the server's vouch, as the tunnel op would.

    Blocking: call it on the worker thread, since the vouch is a context variable that
    a new thread doesn't inherit.

    :raises ConnectError: FAILED_PRECONDITION for a PR the session doesn't link.
    """
    vouch = call.vouch or PullRequestVouch()
    with pr_resource.server_tracking(vouch.server_tracked is True, vouch.default_pr_url):
        try:
            return PULL_REQUEST_OPS[name](reader, call, request)
        except ValueError as exc:
            raise ConnectError(Code.FAILED_PRECONDITION, str(exc)) from exc
