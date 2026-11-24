"""The workspace API's pull request reads, as the host serves them over the tunnel.

Each method runs the same reader code as the host's ``github_*`` tunnel ops, so it
answers with the REST route's payload. The server decides which pull requests belong
to a session: with each call it sends the session and its vouch for the pull request
in the call frame, never in the request message. A call without them isn't served.
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


# One pull request read: the reader, the call and the request, to the REST payload.
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
}


def run_pull_request_op(
    name: str, reader: WorkspaceReader, call: PullRequestCall, request: Any
) -> dict[str, Any]:
    """Run one pull request read under the server's vouch, as the tunnel op would.

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
