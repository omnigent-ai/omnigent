"""The host's pull request reads: served only over the tunnel, for the session's own PRs.

Every case runs through :func:`serve_tunnel_call` or the direct socket, the two ways a
call reaches the host, so the scope and vouch checks are tested where calls arrive.
A provider read past the scope check is a test failure unless the case allows it.
"""

from __future__ import annotations

import copy
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from connectrpc.code import Code

from omnigent import config
from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host.frames import HostWorkspaceCallFrame, PullRequestVouch
from omnigent.host.workspace_methods import WORKSPACE_METHODS
from omnigent.host.workspace_payloads import payload_dict
from omnigent.host.workspace_service import serve_tunnel_call, start_workspace_socket
from omnigent.host.workspace_tokens import mint_token
from omnigent.runner import (
    github_images,
    github_lifecycle,
    github_lookup,
    github_merge_queue,
    github_resource,
    github_reviews,
    github_search,
    github_stack,
    pr_resource,
)
from omnigent.runner.github_images import ImageRefused, PullRequestImage
from omnigent.runner.pull_request_errors import (
    CommentChangedError,
    PullRequestChangedError,
    PullRequestError,
    UnknownOutcomeError,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.connect_stream import connect_frame, end_of_stream_code
from tests.host.test_workspace_methods import (
    _PULL_REQUEST_OWNER_METHODS,
    _PULL_REQUEST_OWNER_STREAMS,
    _PULL_REQUEST_READS,
)

_SESSION = "conv_prs"
_PR = "https://github.com/example/app/pull/7"
_OTHER_PR = "https://github.com/example/app/pull/8"
_HEAD = "a" * 40
_BASE = "b" * 40
_PULL_REQUEST_METHODS = sorted(name for name, m in WORKSPACE_METHODS.items() if m.pull_request)


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def provider_reads(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The PRs a read got past the scope check for; each serves an empty answer."""
    reached: list[str | None] = []

    def serving_facet(_root: str, reference: Any) -> None:
        reached.append(reference.url if reference is not None else None)

    monkeypatch.setattr(pr_resource, "_serving_facet", serving_facet)
    return reached


def _frame(
    root: Path,
    method: str,
    request: bytes = b"",
    *,
    session_id: str | None = _SESSION,
    vouch: PullRequestVouch | None = None,
    account_rights: bool = False,
) -> HostWorkspaceCallFrame:
    return HostWorkspaceCallFrame(
        "r1", str(root), method, request, session_id, vouch, account_rights=account_rights
    )


async def _answer(frame: HostWorkspaceCallFrame) -> tuple[list[bytes], str | None, str | None]:
    messages: list[bytes] = []
    async for reply in serve_tunnel_call(lambda ws: WorkspaceReader(Path(ws)), frame):
        if reply.message is not None:
            messages.append(reply.message)
        if reply.done:
            return messages, reply.error_code, reply.error_message
    raise AssertionError("the call never ended")


def _files_request(pr_url: str = _PR) -> bytes:
    return pb.ListPullRequestFilesRequest(pr_url=pr_url).SerializeToString()


def _record(session_id: str, url: str = _PR) -> None:
    SessionPrRegistry(session_id).record(
        [PullRequestRef.from_url(url)], relationship="attached", source="test"
    )


def _capabilities(messages: list[bytes]) -> set[str]:
    return set(pb.GetCapabilitiesResponse.FromString(messages[0]).capabilities)


@pytest.mark.parametrize("method", _PULL_REQUEST_METHODS)
async def test_a_tunnel_call_without_a_session_reads_no_pull_request(
    tmp_path: Path, method: str, provider_reads: list[str | None]
) -> None:
    """An older server sends no session, so the host serves it no pull request."""
    _record(_SESSION)
    _messages, code, _ = await _answer(_frame(tmp_path, method, session_id=None))

    assert code == Code.UNIMPLEMENTED.value
    assert provider_reads == []


async def test_capabilities_list_pull_requests_only_for_a_call_with_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    with_session, _, _ = await _answer(_frame(tmp_path, "GetCapabilities"))
    without, _, _ = await _answer(_frame(tmp_path, "GetCapabilities", session_id=None))

    assert set(_PULL_REQUEST_METHODS) <= _capabilities(with_session)
    assert not set(_PULL_REQUEST_METHODS) & _capabilities(without)


async def test_the_host_refuses_a_pr_another_session_recorded(
    tmp_path: Path, provider_reads: list[str | None]
) -> None:
    _record("conv_other")
    _messages, code, _ = await _answer(_frame(tmp_path, "ListPullRequestFiles", _files_request()))

    assert code == Code.FAILED_PRECONDITION.value
    assert provider_reads == []


async def test_the_host_reads_a_pr_it_recorded_for_the_session(
    tmp_path: Path, provider_reads: list[str | None]
) -> None:
    _record(_SESSION)
    messages, code, _ = await _answer(_frame(tmp_path, "ListPullRequestFiles", _files_request()))

    assert code is None
    assert provider_reads == [_PR]
    payload = pb.ListPullRequestFilesResponse.FromString(messages[0]).payload
    assert dict(payload) == {"object": "list", "data": [], "has_more": False}


async def test_the_servers_vouch_reaches_the_read_on_its_worker_thread(
    tmp_path: Path, provider_reads: list[str | None]
) -> None:
    """The vouch is a context variable, and the read runs on a new thread: it must arrive."""
    vouch = PullRequestVouch(server_tracked=True)
    _messages, code, _ = await _answer(
        _frame(tmp_path, "ListPullRequestFiles", _files_request(), vouch=vouch)
    )

    assert code is None
    assert provider_reads == [_PR]


async def test_the_servers_default_pr_is_read_when_a_call_names_none(
    tmp_path: Path, provider_reads: list[str | None]
) -> None:
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)
    _messages, code, _ = await _answer(
        _frame(tmp_path, "ListPullRequestFiles", _files_request(""), vouch=vouch)
    )

    assert code is None
    assert provider_reads == [_PR]


_HEAD, _BASE = "a" * 40, "b" * 40


@pytest.mark.parametrize(
    ("request_message", "refusal"),
    [
        (pb.ListPullRequestCommitsRequest(head_sha=_HEAD), "Select a pull request to list"),
        (pb.GetCommitDiffRequest(commit_sha=_HEAD), "Select a pull request to list"),
        (
            pb.ComparePullRequestRangeRequest(head_sha=_HEAD, from_sha=_BASE),
            "Select a pull request to list",
        ),
        (
            pb.ListReviewThreadsRequest(head_sha=_HEAD, base_sha=_BASE),
            "Select a pull request to read",
        ),
        (
            pb.ListReviewThreadCommentsRequest(thread_id="thread_1", page_token="cursor_1"),
            "Select a pull request to read",
        ),
    ],
    ids=["commits", "commit-diff", "range", "threads", "thread-comments"],
)
async def test_the_other_reads_need_the_pr_named_as_rest_does(
    tmp_path: Path, request_message: Any, refusal: str
) -> None:
    """Only the overview, files and diff read the session's first PR when none is named."""
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)
    method = type(request_message).__name__.removesuffix("Request")
    _messages, code, message = await _answer(
        _frame(tmp_path, method, request_message.SerializeToString(), vouch=vouch)
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert message is not None and message.startswith(refusal)


@pytest.mark.parametrize(
    "vouch",
    [
        None,
        PullRequestVouch(server_tracked=False, default_pr_url=_PR),
        # Not a boolean: the REST query strings' spellings never track a PR here.
        PullRequestVouch(server_tracked="1"),  # type: ignore[arg-type]
        PullRequestVouch(server_tracked="true"),  # type: ignore[arg-type]
    ],
    ids=["none", "untracked-default", "string-1", "string-true"],
)
async def test_a_vouch_the_server_did_not_give_serves_no_unrecorded_pr(
    tmp_path: Path, vouch: PullRequestVouch | None, provider_reads: list[str | None]
) -> None:
    """Without the server's vouch, as before it imports, the host's own record decides."""
    # A request message crafted to claim tracking: field 15 is no field of the request.
    crafted = _files_request() + b"\x78\x01"
    _messages, code, message = await _answer(
        _frame(tmp_path, "ListPullRequestFiles", crafted, vouch=vouch)
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert message == "This pull request is not associated with the session"
    assert provider_reads == []


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
async def test_the_direct_socket_serves_no_pull_request_whatever_the_call_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_reads: list[str | None]
) -> None:
    """The socket's token names a workspace, not a session the server authorized."""
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    _record(_SESSION)
    opened: list[str] = []

    def reader_for(workspace: str) -> WorkspaceReader:
        opened.append(workspace)
        return WorkspaceReader(Path(workspace))

    listener = await start_workspace_socket(reader_for, host_id="host_a")
    assert listener is not None
    token, _ = mint_token(
        listener.token_key, host_id="host_a", session_id=_SESSION, workspace=str(tmp_path)
    )
    forged = {
        "Authorization": f"Bearer {token}",
        "X-Omnigent-Session": _SESSION,
        "X-Omnigent-Vouch": '{"server_tracked": true}',
    }
    service = "/omnigent.api.workspace.v1alpha1.WorkspaceService"
    try:
        transport = httpx.AsyncHTTPTransport(uds=str(listener.path))
        async with httpx.AsyncClient(transport=transport, base_url="http://workspace") as http:
            listed = await http.post(f"{service}/GetCapabilities", json={}, headers=forged)
            assert listed.status_code == 200, listed.text
            assert not set(_PULL_REQUEST_METHODS) & set(listed.json()["capabilities"])
            for method in _PULL_REQUEST_METHODS:
                request = _naming_the_pr(method)
                binary = request.SerializeToString() + b"\x78\x01"
                if WORKSPACE_METHODS[method].streaming:
                    assert await _stream_error(http, f"{service}/{method}", binary, forged) == (
                        "unimplemented"
                    ), method
                    continue
                response = await http.post(
                    f"{service}/{method}",
                    content=binary,
                    headers={**forged, "Content-Type": "application/proto"},
                )
                assert response.status_code == 501, (method, response.text)
    finally:
        await listener.stop()

    assert opened == []
    assert provider_reads == []


def _naming_the_pr(method: str) -> Any:
    """A request of ``method`` that names ``_PR``, in ``pr_url`` or in a batch's list."""
    request_type = WORKSPACE_METHODS[method].request_type
    if "pr_url" in request_type.DESCRIPTOR.fields_by_name:
        return request_type(pr_url=_PR)
    if "pr_urls" in request_type.DESCRIPTOR.fields_by_name:
        return request_type(pr_urls=[_PR])
    if "pull_requests" in request_type.DESCRIPTOR.fields_by_name:
        return request_type(pull_requests=[{"pr_url": _PR}])
    # A branch list names none; it is served only with the server's call frame all the same.
    return request_type(branch="feature/login")


async def _stream_error(
    http: httpx.AsyncClient, path: str, message: bytes, headers: dict[str, str]
) -> str | None:
    """A Connect streaming call's error code, from its end-of-stream message."""
    response = await http.post(
        path,
        content=connect_frame(message),
        headers={**headers, "Content-Type": "application/connect+proto"},
    )
    return end_of_stream_code(response.content)


def _pr_target(**fields: Any) -> dict[str, Any]:
    return {"session_id": _SESSION, "pr_url": _PR, **fields}


# A PNG signature: the runner refuses an upload whose bytes are no image or video.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
# Each owner method: a valid request, the pr_resource function it runs, and its arguments.
_OWNER_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "ListLabels": (pb.ListLabelsRequest(pr_url=_PR), "pr_labels", _pr_target(text="")),
    "ListReviewerCandidates": (
        pb.ListReviewerCandidatesRequest(pr_url=_PR),
        "pr_review_candidates",
        _pr_target(),
    ),
    "GetMergeOptions": (pb.GetMergeOptionsRequest(pr_url=_PR), "pr_merge_options", _pr_target()),
    "CreateComment": (
        pb.CreateCommentRequest(pr_url=_PR, body="Looks good"),
        "create_pr_comment",
        _pr_target(body="Looks good"),
    ),
    "CreateReviewThread": (
        pb.CreateReviewThreadRequest(
            pr_url=_PR,
            body="Why this?",
            anchor=pb.ReviewThreadAnchor(
                path="src/app.py",
                side="RIGHT",
                line=4,
                start_line=2,
                head_sha=_HEAD,
                base_sha=_BASE,
            ),
        ),
        "create_pr_review_thread",
        _pr_target(
            body="Why this?",
            anchor={
                "path": "src/app.py",
                "side": "RIGHT",
                "line": 4,
                "start_line": 2,
                "head_sha": _HEAD,
                "base_sha": _BASE,
            },
        ),
    ),
    "ReplyToReviewThread": (
        pb.ReplyToReviewThreadRequest(pr_url=_PR, thread_id="thread_1", body="Done"),
        "reply_to_pr_review_thread",
        _pr_target(thread_id="thread_1", body="Done"),
    ),
    "UpdatePullRequestDetails": (
        pb.UpdatePullRequestDetailsRequest(pr_url=_PR, title="A clearer title"),
        "update_pr_details",
        _pr_target(title="A clearer title", body=None),
    ),
    "UpdateLabels": (
        pb.UpdateLabelsRequest(pr_url=_PR, add=["bug"], remove=["wip"]),
        "update_pr_labels",
        _pr_target(add=["bug"], remove=["wip"]),
    ),
    "UpdateReviewers": (
        pb.UpdateReviewersRequest(
            pr_url=_PR,
            add=[pb.Reviewer(login="octocat", kind="user")],
            remove=[pb.Reviewer(login="core", kind="team")],
        ),
        "update_pr_reviewers",
        _pr_target(
            add=[{"login": "octocat", "kind": "user"}], remove=[{"login": "core", "kind": "team"}]
        ),
    ),
    "SetPullRequestAccount": (
        pb.SetPullRequestAccountRequest(pr_url=_PR, account="octocat"),
        "set_pr_preference",
        _pr_target(account="octocat", remote=None),
    ),
    "MergePullRequest": (
        pb.MergePullRequestRequest(pr_url=_PR, merge_method="squash", expected_head_oid=_HEAD),
        "merge_pr",
        _pr_target(merge_method="squash", expected_head=_HEAD),
    ),
    "MergePullRequestAsAccount": (
        pb.MergePullRequestAsAccountRequest(
            pr_url=_PR, merge_method="squash", expected_head_oid=_HEAD, account="octocat"
        ),
        "merge_pr_as",
        _pr_target(merge_method="squash", expected_head=_HEAD, account="octocat"),
    ),
    "DisableAutoMerge": (
        pb.DisableAutoMergeRequest(pr_url=_PR),
        "disable_pr_auto_merge",
        _pr_target(),
    ),
    "UploadAttachment": (
        pb.UploadAttachmentRequest(pr_url=_PR, name="shot.png", content=_PNG),
        "upload_pr_attachment",
        _pr_target(name="shot.png", content=_PNG),
    ),
}


_DIGEST = "c" * 64
_ANCHOR = pb.ReviewThreadAnchor(
    path="src/app.py", side="RIGHT", line=4, head_sha=_HEAD, base_sha=_BASE
)
# Each review and lifecycle change of one pull request: a valid request, the github_reviews or
# github_lifecycle function it runs, and its arguments.
_GITHUB_CHANGE_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "GetPendingReview": (pb.GetPendingReviewRequest(pr_url=_PR), "get_pending_review", {}),
    "StartPendingReview": (
        pb.StartPendingReviewRequest(pr_url=_PR, commit_oid=_HEAD),
        "start_pending_review",
        {"commit_oid": _HEAD},
    ),
    "AddPendingReviewComment": (
        pb.AddPendingReviewCommentRequest(
            pr_url=_PR,
            body="Why?",
            anchor=_ANCHOR,
            pending_review_id="PRR_1",
            expected_head_oid=_HEAD,
        ),
        "add_pending_review_comment",
        {
            "body": "Why?",
            "anchor": {
                "path": "src/app.py",
                "side": "RIGHT",
                "line": 4,
                "start_line": None,
                "head_sha": _HEAD,
                "base_sha": _BASE,
            },
            "pending_review_id": "PRR_1",
            "expected_head": _HEAD,
        },
    ),
    "UpdatePendingReviewComment": (
        pb.UpdatePendingReviewCommentRequest(
            pr_url=_PR,
            comment_node_id="PRRC_1",
            body="Better",
            expected_body_sha256=_DIGEST,
            pending_review_id="PRR_1",
        ),
        "update_pending_review_comment",
        {
            "comment_node_id": "PRRC_1",
            "body": "Better",
            "expected_body_sha256": _DIGEST,
            "pending_review_id": "PRR_1",
        },
    ),
    "DeletePendingReviewComment": (
        pb.DeletePendingReviewCommentRequest(
            pr_url=_PR, comment_node_id="PRRC_1", pending_review_id="PRR_1"
        ),
        "delete_pending_review_comment",
        {"comment_node_id": "PRRC_1", "pending_review_id": "PRR_1"},
    ),
    "SubmitPendingReview": (
        pb.SubmitPendingReviewRequest(
            pr_url=_PR, event="APPROVE", body="", commit_oid=_HEAD, pending_review_id="PRR_1"
        ),
        "submit_pending_review",
        {"event": "APPROVE", "body": "", "commit_oid": _HEAD, "pending_review_id": "PRR_1"},
    ),
    "DiscardPendingReview": (
        pb.DiscardPendingReviewRequest(pr_url=_PR, pending_review_id="PRR_1"),
        "discard_pending_review",
        {"pending_review_id": "PRR_1"},
    ),
    "SubmitReview": (
        pb.SubmitReviewRequest(pr_url=_PR, event="COMMENT", body="Nit", commit_oid=_HEAD),
        "submit_review",
        {"event": "COMMENT", "body": "Nit", "commit_oid": _HEAD},
    ),
    "SubmitReviewVerdict": (
        pb.SubmitReviewVerdictRequest(
            pr_url=_PR, event="REQUEST_CHANGES", body="Fix", commit_oid=_HEAD
        ),
        "submit_review",
        {"event": "REQUEST_CHANGES", "body": "Fix", "commit_oid": _HEAD},
    ),
    "SetReviewThreadResolved": (
        pb.SetReviewThreadResolvedRequest(pr_url=_PR, thread_id="PRRT_1", resolved=True),
        "set_thread_resolved",
        {"thread_id": "PRRT_1", "resolved": True},
    ),
    "EditComment": (
        pb.EditCommentRequest(
            pr_url=_PR, kind="review", comment_id=5, body="Mine", expected_body_sha256=_DIGEST
        ),
        "edit_comment",
        {"kind": "review", "comment_id": 5, "body": "Mine", "expected_body_sha256": _DIGEST},
    ),
    "DeleteComment": (
        pb.DeleteCommentRequest(pr_url=_PR, kind="issue", comment_id=5),
        "delete_comment",
        {"kind": "issue", "comment_id": 5},
    ),
    "AddReaction": (
        pb.AddReactionRequest(pr_url=_PR, kind="issue", comment_id=5, content="HEART"),
        "add_reaction",
        {"kind": "issue", "comment_id": 5, "content": "HEART"},
    ),
    "RemoveReaction": (
        pb.RemoveReactionRequest(pr_url=_PR, kind="review", comment_id=5, content="HEART"),
        "remove_reaction",
        {"kind": "review", "comment_id": 5, "content": "HEART"},
    ),
    "SetPullRequestState": (
        pb.SetPullRequestStateRequest(pr_url=_PR, state="closed"),
        "set_state",
        {"state": "closed"},
    ),
    "SetPullRequestDraft": (
        pb.SetPullRequestDraftRequest(pr_url=_PR, draft=True),
        "set_draft",
        {"draft": True},
    ),
}
# The batch change, which names its pull requests in ``pull_requests``.
_AUTO_MERGE = pb.EnableAutoMergeRequest(
    pull_requests=[
        pb.PullRequestAutoMerge(pr_url=_PR, merge_method="squash", expected_head_oid=_HEAD),
        pb.PullRequestAutoMerge(pr_url=_OTHER_PR, merge_method="rebase", expected_head_oid=_BASE),
    ]
)


# Each merge queue method: a valid request, the github_merge_queue function, and its arguments.
_QUEUE_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "GetMergeQueue": (pb.GetMergeQueueRequest(pr_url=_PR), "get_merge_queue", {}),
    "EnqueuePullRequest": (
        pb.EnqueuePullRequestRequest(pr_url=_PR, expected_head_oid=_HEAD),
        "enqueue",
        {"expected_head_oid": _HEAD},
    ),
    "DequeuePullRequest": (pb.DequeuePullRequestRequest(pr_url=_PR), "dequeue", {}),
}
# The stack read: a valid request, the github_stack function it runs, and its arguments.
_STACK_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "GetPullRequestStack": (pb.GetPullRequestStackRequest(pr_url=_PR), "pull_request_stack", {}),
}
# The searches for the owner's reviewer picker and editor: a request, its function, arguments.
_SEARCH_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "SearchReviewers": (
        pb.SearchReviewersRequest(pr_url=_PR, text="al", page_size=5),
        "search_reviewers",
        {"text": "al", "page_size": 5},
    ),
    "SearchCompletions": (
        pb.SearchCompletionsRequest(pr_url=_PR, kind=pb.COMPLETION_KIND_MENTION, text="al"),
        "search_completions",
        {"kind": "MENTION", "text": "al"},
    ),
}
# The methods that act on GitHub for the session's selected pull request.
_SELECTED_PR_CALLS = {**_GITHUB_CHANGE_CALLS, **_QUEUE_CALLS, **_STACK_CALLS, **_SEARCH_CALLS}
# The methods that read pull requests by URL or branch alone: a request, its function, arguments.
_FINDER_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "LookupPullRequest": (
        pb.LookupPullRequestRequest(pr_url=_OTHER_PR),
        "lookup_pull_request",
        {"pr_url": _OTHER_PR},
    ),
    "GetPullRequestListStats": (
        pb.GetPullRequestListStatsRequest(pr_urls=[_PR, _OTHER_PR]),
        "pull_request_list_stats",
        {"pr_urls": [_PR, _OTHER_PR]},
    ),
    "ListBranchPullRequests": (
        pb.ListBranchPullRequestsRequest(branch="feature/login"),
        "branch_pull_requests",
        {"branch": "feature/login"},
    ),
}


def record_github_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Each GitHub call a method made: the function, its root, PR and arguments."""
    calls: list[tuple[str, str, str, dict[str, Any]]] = []
    for table in (_GITHUB_CHANGE_CALLS, _QUEUE_CALLS, _STACK_CALLS, _SEARCH_CALLS):
        for function in {name for _request, name, _kwargs in table.values()}:
            if table is _QUEUE_CALLS:
                module = github_merge_queue
            elif table is _STACK_CALLS:
                module = github_stack
            elif table is _SEARCH_CALLS:
                module = github_search
            else:
                module = github_reviews if hasattr(github_reviews, function) else github_lifecycle

            def record(
                root: str, reference: PullRequestRef, *, _function: str = function, **kwargs: Any
            ) -> dict[str, Any]:
                calls.append((_function, root, reference.url, kwargs))
                return {"object": "test"}

            monkeypatch.setattr(module, function, record)
    return calls


@pytest.fixture
def github_changes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, dict[str, Any]]]:
    return record_github_changes(monkeypatch)


def record_owner_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Each pr_resource call an owner method made: the function, its root and arguments."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for function in {name for _request, name, _kwargs in _OWNER_CALLS.values()}:

        def record(root: str, *, _function: str = function, **kwargs: Any) -> dict[str, Any]:
            calls.append((_function, root, kwargs))
            return {"object": "test"}

        monkeypatch.setattr(pr_resource, function, record)
    return calls


@pytest.fixture
def owner_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    return record_owner_calls(monkeypatch)


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The providers a call asked for a facet, so a refused call can show it reached none."""
    asked: list[str | None] = []

    def facet(provider_id: str | None) -> None:
        asked.append(provider_id)

    monkeypatch.setattr(pr_resource, "_facet", facet)
    return asked


def test_every_owner_method_has_a_call_case() -> None:
    assert not set(_OWNER_CALLS) & set(_SELECTED_PR_CALLS)
    assert not set(_GITHUB_CHANGE_CALLS) & set(_QUEUE_CALLS)
    assert not set(_GITHUB_CHANGE_CALLS) & set(_STACK_CALLS)
    assert not set(_QUEUE_CALLS) & set(_STACK_CALLS)
    assert not set(_SEARCH_CALLS) & (
        set(_GITHUB_CHANGE_CALLS) | set(_QUEUE_CALLS) | set(_STACK_CALLS)
    )
    owner_unary = _PULL_REQUEST_OWNER_METHODS - _PULL_REQUEST_OWNER_STREAMS
    assert not set(_FINDER_CALLS) & (set(_OWNER_CALLS) | set(_SELECTED_PR_CALLS))
    assert (
        set(_OWNER_CALLS) | set(_SELECTED_PR_CALLS) | set(_FINDER_CALLS) | {"EnableAutoMerge"}
        == owner_unary
    )


@pytest.mark.parametrize("method", sorted(_OWNER_CALLS))
async def test_an_owner_method_runs_its_rest_function_with_the_requests_fields(
    tmp_path: Path, method: str, owner_calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    request, function, kwargs = _OWNER_CALLS[method]
    messages, code, message = await _answer(_frame(tmp_path, method, request.SerializeToString()))

    assert code is None, message
    assert owner_calls == [(function, str(tmp_path.resolve()), kwargs)]
    payload = WORKSPACE_METHODS[method].response_type.FromString(messages[0]).payload
    assert dict(payload) == {"object": "test"}


@pytest.mark.parametrize(
    ("method", "request_message", "kwargs"),
    [
        (
            "UpdatePullRequestDetails",
            pb.UpdatePullRequestDetailsRequest(pr_url=_PR, body=""),
            _pr_target(title=None, body=""),
        ),
        (
            "UpdatePullRequestDetails",
            pb.UpdatePullRequestDetailsRequest(pr_url=_PR, title=""),
            _pr_target(title="", body=None),
        ),
        (
            "UpdatePullRequestDetails",
            pb.UpdatePullRequestDetailsRequest(pr_url=_PR),
            _pr_target(title=None, body=None),
        ),
        (
            "CreateReviewThread",
            pb.CreateReviewThreadRequest(
                pr_url=_PR,
                body="Why this?",
                anchor=pb.ReviewThreadAnchor(
                    path="src/app.py", side="LEFT", line=4, head_sha=_HEAD, base_sha=_BASE
                ),
            ),
            _pr_target(
                body="Why this?",
                anchor={
                    "path": "src/app.py",
                    "side": "LEFT",
                    "line": 4,
                    "start_line": None,
                    "head_sha": _HEAD,
                    "base_sha": _BASE,
                },
            ),
        ),
        (
            "CreateReviewThread",
            pb.CreateReviewThreadRequest(pr_url=_PR, body="Why this?"),
            _pr_target(body="Why this?", anchor=None),
        ),
    ],
    ids=["empty-body", "empty-title", "neither", "one-line", "no-anchor"],
)
async def test_an_unset_field_stays_unset_and_an_empty_one_is_passed_on(
    tmp_path: Path,
    method: str,
    request_message: Any,
    kwargs: dict[str, Any],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """``pr_resource`` tells "keep it" (``None``) from "make it empty", and refuses the latter."""
    _messages, code, message = await _answer(
        _frame(tmp_path, method, request_message.SerializeToString())
    )

    assert code is None, message
    assert [call[2] for call in owner_calls] == [kwargs]


@pytest.mark.parametrize("method", sorted(_OWNER_CALLS))
async def test_an_owner_method_refuses_a_pr_the_session_never_recorded(
    tmp_path: Path, method: str, providers: list[str | None]
) -> None:
    """Without the server's vouch, no change reaches a provider for an unrecorded PR."""
    request, _function, _kwargs = _OWNER_CALLS[method]
    _messages, code, message = await _answer(_frame(tmp_path, method, request.SerializeToString()))

    assert code == Code.FAILED_PRECONDITION.value
    assert message == "This pull request is not associated with the session"
    assert providers == []


# SetPullRequestAccount refuses a missing URL earlier, as its own cases show.
@pytest.mark.parametrize("method", sorted(set(_OWNER_CALLS) - {"SetPullRequestAccount"}))
async def test_an_owner_method_naming_no_pr_changes_none_the_server_offers_by_default(
    tmp_path: Path, method: str, providers: list[str | None]
) -> None:
    """A vouch's default PR serves reads; a change must name the PR it changes."""
    request, _function, _kwargs = _OWNER_CALLS[method]
    unnamed = type(request)()
    unnamed.CopyFrom(request)
    unnamed.ClearField("pr_url")
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)

    _messages, code, message = await _answer(
        _frame(tmp_path, method, unnamed.SerializeToString(), vouch=vouch)
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert message is not None and message.startswith("Select a pull request to ")
    assert providers == []


async def test_list_labels_searches_the_selected_prs_repository_by_text(
    tmp_path: Path, owner_calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    request = pb.ListLabelsRequest(pr_url=_PR, text="bug")
    _messages, code, message = await _answer(
        _frame(tmp_path, "ListLabels", request.SerializeToString())
    )

    assert (code, message) == (None, None)
    assert owner_calls == [("pr_labels", str(tmp_path.resolve()), _pr_target(text="bug"))]


async def test_list_labels_refuses_a_search_longer_than_the_cap(
    tmp_path: Path, owner_calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    request = pb.ListLabelsRequest(pr_url=_PR, text="a" * 257)
    _messages, code, message = await _answer(
        _frame(tmp_path, "ListLabels", request.SerializeToString())
    )

    assert code == Code.INVALID_ARGUMENT.value
    assert message == "The label search is longer than 256 characters"
    assert owner_calls == []


_VOUCHED = PullRequestVouch(server_tracked=True)


async def _reason(frame: HostWorkspaceCallFrame) -> tuple[str | None, str | None]:
    """A call's end: its error code and the refusal reason the reply carries."""
    async for reply in serve_tunnel_call(lambda ws: WorkspaceReader(Path(ws)), frame):
        if reply.done:
            return reply.error_code, reply.error_reason
    raise AssertionError("the call never ended")


@pytest.mark.parametrize("method", sorted(_SELECTED_PR_CALLS))
async def test_a_github_method_runs_its_github_change_with_the_requests_fields(
    tmp_path: Path, method: str, github_changes: list[tuple[str, str, str, dict[str, Any]]]
) -> None:
    request, function, kwargs = _SELECTED_PR_CALLS[method]
    messages, code, message = await _answer(
        _frame(tmp_path, method, request.SerializeToString(), vouch=_VOUCHED)
    )

    assert code is None, message
    assert github_changes == [(function, str(tmp_path.resolve()), _PR, kwargs)]
    payload = WORKSPACE_METHODS[method].response_type.FromString(messages[0]).payload
    assert dict(payload) == {"object": "test"}


def record_auto_merges(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str, str]]]:
    """Each auto-merge batch the host sent: its pull requests, methods and heads."""
    batches: list[list[tuple[str, str, str]]] = []

    def enable(_root: str, items: list[github_lifecycle.AutoMergeItem]) -> dict[str, Any]:
        batches.append(
            [(item.reference.url, item.merge_method, item.expected_head_oid) for item in items]
        )
        return {"object": "test"}

    monkeypatch.setattr(github_lifecycle, "enable_auto_merge", enable)
    return batches


@pytest.fixture
def auto_merges(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str, str]]]:
    return record_auto_merges(monkeypatch)


async def test_auto_merge_sends_every_pull_request_the_batch_names(
    tmp_path: Path, auto_merges: list[list[tuple[str, str, str]]]
) -> None:
    messages, code, message = await _answer(
        _frame(tmp_path, "EnableAutoMerge", _AUTO_MERGE.SerializeToString(), vouch=_VOUCHED)
    )

    assert code is None, message
    assert auto_merges == [[(_PR, "squash", _HEAD), (_OTHER_PR, "rebase", _BASE)]]
    assert dict(pb.EnableAutoMergeResponse.FromString(messages[0]).payload) == {"object": "test"}


async def test_auto_merge_sends_none_when_the_session_never_recorded_one_of_them(
    tmp_path: Path, auto_merges: list[list[tuple[str, str, str]]]
) -> None:
    _record(_SESSION, _PR)

    _messages, code, message = await _answer(
        _frame(tmp_path, "EnableAutoMerge", _AUTO_MERGE.SerializeToString())
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert message == "This pull request is not associated with the session"
    assert auto_merges == []


@pytest.mark.parametrize(
    "items, refusal",
    [
        ([], "Enable auto-merge for 1 to 20 pull requests at once"),
        (
            [pb.PullRequestAutoMerge(pr_url=_PR, merge_method="squash", expected_head_oid=_HEAD)]
            * 21,
            "Enable auto-merge for 1 to 20 pull requests at once",
        ),
        (
            [pb.PullRequestAutoMerge(merge_method="squash", expected_head_oid=_HEAD)],
            "Name each pull request to merge automatically",
        ),
        (
            [pb.PullRequestAutoMerge(pr_url=_PR, merge_method="fast-forward")],
            "Expected merge_method to be merge, squash, or rebase",
        ),
        (
            [pb.PullRequestAutoMerge(pr_url=_PR, merge_method="squash", expected_head_oid="x")],
            "Expected expected_head_oid to be a full commit SHA",
        ),
        (
            [pb.PullRequestAutoMerge(pr_url=_PR, merge_method="squash", expected_head_oid=_HEAD)]
            * 2,
            "Name each pull request once",
        ),
    ],
    ids=["none", "too-many", "unnamed", "bad-method", "bad-head", "repeated"],
)
async def test_a_malformed_auto_merge_batch_is_refused_with_nothing_sent(
    tmp_path: Path,
    auto_merges: list[list[tuple[str, str, str]]],
    items: list[pb.PullRequestAutoMerge],
    refusal: str,
) -> None:
    request = pb.EnableAutoMergeRequest(pull_requests=items)
    _messages, code, message = await _answer(
        _frame(tmp_path, "EnableAutoMerge", request.SerializeToString(), vouch=_VOUCHED)
    )

    assert (code, message) == (Code.INVALID_ARGUMENT.value, refusal)
    assert auto_merges == []


async def test_a_draft_reply_goes_in_its_conversation_without_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_changes: list[tuple[str, str, str, dict[str, Any]]] = []

    def reply(root: str, reference: PullRequestRef, **kwargs: Any) -> dict[str, Any]:
        github_changes.append(("add_pending_review_reply", root, reference.url, kwargs))
        return {"object": "test"}

    monkeypatch.setattr(github_reviews, "add_pending_review_reply", reply)
    request = pb.AddPendingReviewCommentRequest(
        pr_url=_PR,
        body="Agreed",
        in_reply_to_thread_id="PRRT_1",
        pending_review_id="PRR_1",
        expected_head_oid=_HEAD,
    )
    _messages, code, message = await _answer(
        _frame(tmp_path, "AddPendingReviewComment", request.SerializeToString(), vouch=_VOUCHED)
    )

    assert code is None, message
    assert github_changes == [
        (
            "add_pending_review_reply",
            str(tmp_path.resolve()),
            _PR,
            {
                "body": "Agreed",
                "thread_id": "PRRT_1",
                "pending_review_id": "PRR_1",
                "expected_head": _HEAD,
            },
        )
    ]


@pytest.mark.parametrize(
    ("request_message", "refusal"),
    [
        (
            pb.SearchReviewersRequest(pr_url=_PR, text="x" * 257),
            "The search is longer than 256 characters",
        ),
        (
            pb.SearchReviewersRequest(pr_url="https://github.com/" + "x" * 2048, text="al"),
            "The pull request URL is longer than 2048 characters",
        ),
        (pb.SearchReviewersRequest(pr_url=_PR, page_size=21), "Ask for at most 20 reviewers"),
        (pb.SearchReviewersRequest(pr_url=_PR, page_size=-1), "Ask for at most 20 reviewers"),
        (
            pb.SearchCompletionsRequest(
                pr_url=_PR, kind=pb.COMPLETION_KIND_MENTION, text="x" * 257
            ),
            "The search is longer than 256 characters",
        ),
        (
            pb.SearchCompletionsRequest(pr_url=_PR, text="al"),
            "Choose what to complete: people or pull requests",
        ),
        (
            pb.SearchCompletionsRequest(pr_url=_PR, kind=99, text="al"),
            "Choose what to complete: people or pull requests",
        ),
    ],
    ids=[
        "long-reviewer-text",
        "long-pr-url",
        "too-many-reviewers",
        "negative-page",
        "long-completion-text",
        "no-kind",
        "unknown-kind",
    ],
)
async def test_a_malformed_search_is_refused_with_nothing_read(
    tmp_path: Path,
    github_changes: list[tuple[str, str, str, dict[str, Any]]],
    request_message: Any,
    refusal: str,
) -> None:
    method = type(request_message).__name__.removesuffix("Request")
    _messages, code, message = await _answer(
        _frame(tmp_path, method, request_message.SerializeToString(), vouch=_VOUCHED)
    )

    assert (code, message) == (Code.INVALID_ARGUMENT.value, refusal)
    assert github_changes == []


async def test_a_reference_completion_passes_its_kind_to_the_search(
    tmp_path: Path, github_changes: list[tuple[str, str, str, dict[str, Any]]]
) -> None:
    request = pb.SearchCompletionsRequest(
        pr_url=_PR, kind=pb.COMPLETION_KIND_REFERENCE, text="fix"
    )
    _messages, code, message = await _answer(
        _frame(tmp_path, "SearchCompletions", request.SerializeToString(), vouch=_VOUCHED)
    )

    assert code is None, message
    assert [call[3] for call in github_changes] == [{"kind": "REFERENCE", "text": "fix"}]


def record_finder_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Each finder's runner call: the function, its root and arguments."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for function in {name for _request, name, _kwargs in _FINDER_CALLS.values()}:

        def record(root: str, *, _function: str = function, **kwargs: Any) -> dict[str, Any]:
            calls.append((_function, root, kwargs))
            return {"object": "test"}

        module = github_search if function == "branch_pull_requests" else github_lookup
        monkeypatch.setattr(module, function, record)
    return calls


@pytest.fixture
def finder_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    return record_finder_calls(monkeypatch)


@pytest.mark.parametrize("vouch", [None, _VOUCHED], ids=["no-vouch", "vouched"])
@pytest.mark.parametrize("method", sorted(_FINDER_CALLS))
async def test_a_finder_reads_the_requests_urls_for_a_session_that_links_none(
    tmp_path: Path,
    method: str,
    vouch: PullRequestVouch | None,
    finder_calls: list[tuple[str, str, dict[str, Any]]],
    provider_reads: list[str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads no PR of the session's: no record, vouch or selected PR is consulted."""
    selected: list[Any] = []
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda *args: selected.append(args))
    request, function, kwargs = _FINDER_CALLS[method]
    messages, code, message = await _answer(
        _frame(tmp_path, method, request.SerializeToString(), vouch=vouch)
    )

    assert code is None, message
    assert finder_calls == [(function, str(tmp_path.resolve()), kwargs)]
    payload = WORKSPACE_METHODS[method].response_type.FromString(messages[0]).payload
    assert payload_dict(payload) == {"object": "test"}
    assert selected == []
    assert provider_reads == []


@pytest.mark.parametrize(
    ("request_message", "refusal"),
    [
        (pb.LookupPullRequestRequest(), "Expected a pull request URL"),
        (
            pb.LookupPullRequestRequest(pr_url="https://github.com/" + "x" * 2048),
            "The pull request URL is longer than 2048 characters",
        ),
        (pb.GetPullRequestListStatsRequest(), "Ask for 1 to 50 pull requests"),
        (
            pb.GetPullRequestListStatsRequest(pr_urls=[_PR] * 51),
            "Ask for 1 to 50 pull requests",
        ),
        (pb.GetPullRequestListStatsRequest(pr_urls=[_PR, ""]), "Expected a pull request URL"),
    ],
    ids=["lookup-none", "lookup-long", "stats-none", "stats-too-many", "stats-empty-url"],
)
async def test_a_finder_naming_no_usable_url_is_refused_with_nothing_read(
    tmp_path: Path,
    finder_calls: list[tuple[str, str, dict[str, Any]]],
    request_message: Any,
    refusal: str,
) -> None:
    method = type(request_message).__name__.removesuffix("Request")
    _messages, code, message = await _answer(
        _frame(tmp_path, method, request_message.SerializeToString())
    )

    assert (code, message) == (Code.INVALID_ARGUMENT.value, refusal)
    assert finder_calls == []


async def test_a_branch_list_naming_no_branch_is_refused_with_nothing_read(
    tmp_path: Path, finder_calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """The host refuses a malformed branch itself; the rules are in the input tests."""
    request = pb.ListBranchPullRequestsRequest(branch="a..b")
    _messages, code, message = await _answer(
        _frame(tmp_path, "ListBranchPullRequests", request.SerializeToString(), vouch=_VOUCHED)
    )

    assert code == Code.INVALID_ARGUMENT.value
    assert message is not None and message.startswith("branch name must not contain '..'")
    assert finder_calls == []


async def test_a_branch_list_github_cant_answer_is_a_precondition_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unlisted(_root: str, **_kwargs: Any) -> dict[str, Any]:
        raise PullRequestError("GitHub couldn't list this branch's pull requests. Try again.")

    monkeypatch.setattr(github_search, "branch_pull_requests", unlisted)
    request = pb.ListBranchPullRequestsRequest(branch="main")
    _messages, code, message = await _answer(
        _frame(tmp_path, "ListBranchPullRequests", request.SerializeToString())
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert message == "GitHub couldn't list this branch's pull requests. Try again."


@pytest.mark.parametrize("method", sorted(_SELECTED_PR_CALLS))
async def test_a_github_method_refuses_a_pr_the_session_never_recorded(
    tmp_path: Path, method: str, github_changes: list[tuple[str, str, str, dict[str, Any]]]
) -> None:
    request, _function, _kwargs = _SELECTED_PR_CALLS[method]
    _messages, code, message = await _answer(_frame(tmp_path, method, request.SerializeToString()))

    assert code == Code.FAILED_PRECONDITION.value
    assert message == "This pull request is not associated with the session"
    assert github_changes == []


@pytest.mark.parametrize("method", sorted(_SELECTED_PR_CALLS))
async def test_a_github_method_naming_no_pr_changes_none_the_server_offers_by_default(
    tmp_path: Path, method: str, github_changes: list[tuple[str, str, str, dict[str, Any]]]
) -> None:
    request, _function, _kwargs = _SELECTED_PR_CALLS[method]
    unnamed = type(request)()
    unnamed.CopyFrom(request)
    unnamed.ClearField("pr_url")
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)

    _messages, code, message = await _answer(
        _frame(tmp_path, method, unnamed.SerializeToString(), vouch=vouch)
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert message is not None and message.startswith("Select a pull request to ")
    assert github_changes == []


_MALFORMED_REVIEWS = {
    "verdict-on-comment-review": pb.SubmitReviewRequest(
        pr_url=_PR, event="APPROVE", body="x", commit_oid=_HEAD
    ),
    "comment-as-verdict": pb.SubmitReviewVerdictRequest(
        pr_url=_PR, event="COMMENT", body="x", commit_oid=_HEAD
    ),
    "unknown-pending-event": pb.SubmitPendingReviewRequest(
        pr_url=_PR, event="DISMISS", commit_oid=_HEAD, pending_review_id="PRR_1"
    ),
    "no-pending-review": pb.DiscardPendingReviewRequest(pr_url=_PR),
    "bad-thread-id": pb.SetReviewThreadResolvedRequest(pr_url=_PR, thread_id="../x"),
    "bad-reply-thread": pb.AddPendingReviewCommentRequest(
        pr_url=_PR,
        body="x",
        in_reply_to_thread_id="a b",
        pending_review_id="PRR_1",
        expected_head_oid=_HEAD,
    ),
    "bad-kind": pb.DeleteCommentRequest(pr_url=_PR, kind="commit", comment_id=5),
    "no-comment-id": pb.DeleteCommentRequest(pr_url=_PR, kind="issue"),
    "bad-reaction": pb.AddReactionRequest(pr_url=_PR, kind="issue", comment_id=5, content="MEH"),
    "bad-digest": pb.EditCommentRequest(
        pr_url=_PR, kind="issue", comment_id=5, body="x", expected_body_sha256="abc"
    ),
    "short-head": pb.StartPendingReviewRequest(pr_url=_PR, commit_oid="abc123"),
    "empty-body": pb.EditCommentRequest(
        pr_url=_PR, kind="issue", comment_id=5, body=" ", expected_body_sha256=_DIGEST
    ),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED_REVIEWS))
async def test_a_malformed_review_change_is_refused_as_invalid_input_before_github(
    tmp_path: Path, case: str, github_changes: list[tuple[str, str, str, dict[str, Any]]]
) -> None:
    request = _MALFORMED_REVIEWS[case]
    method = request.DESCRIPTOR.name.removesuffix("Request")
    code, reason = await _reason(
        _frame(tmp_path, method, request.SerializeToString(), vouch=_VOUCHED)
    )

    assert (code, reason) == (Code.INVALID_ARGUMENT.value, "invalid_input")
    assert github_changes == []


@pytest.mark.parametrize(
    ("failure", "code", "reason"),
    [
        (CommentChangedError(), Code.FAILED_PRECONDITION, "comment_changed"),
        (PullRequestChangedError(), Code.FAILED_PRECONDITION, "pull_request_changed"),
        (
            PullRequestError("Only your own comments can be changed here."),
            Code.FAILED_PRECONDITION,
            None,
        ),
        (UnknownOutcomeError(), Code.UNKNOWN, None),
    ],
    ids=["comment-changed", "head-moved", "not-yours", "unconfirmed"],
)
async def test_a_review_failure_reaches_the_server_with_its_code_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: ValueError,
    code: Code,
    reason: str | None,
) -> None:
    def fail(_root: str, _reference: PullRequestRef, **_kwargs: Any) -> dict[str, Any]:
        raise failure

    monkeypatch.setattr(github_reviews, "edit_comment", fail)
    request, _function, _kwargs = _GITHUB_CHANGE_CALLS["EditComment"]

    assert await _reason(
        _frame(tmp_path, "EditComment", request.SerializeToString(), vouch=_VOUCHED)
    ) == (code.value, reason)


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"name": "a" * 256, "content": b"png"}, "The file's name is longer than 255 characters"),
        ({"name": "shot.png"}, "The attachment is empty"),
    ],
    ids=["long-name", "empty"],
)
async def test_the_host_refuses_an_upload_the_server_would_have(
    tmp_path: Path,
    owner_calls: list[tuple[str, str, dict[str, Any]]],
    fields: dict[str, Any],
    refusal: str,
) -> None:
    request = pb.UploadAttachmentRequest(pr_url=_PR, **fields)
    _messages, code, message = await _answer(
        _frame(tmp_path, "UploadAttachment", request.SerializeToString())
    )

    assert (code, message) == (Code.INVALID_ARGUMENT.value, refusal)
    assert owner_calls == []


_IMAGE_URL = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa"
# Over two 256 KiB parts, so the answer takes three.
_IMAGE = PullRequestImage(content_type="image/png", data=_PNG + bytes(range(256)) * 2100)


def _image_frame(root: Path, **fields: Any) -> HostWorkspaceCallFrame:
    request = pb.ReadPullRequestImageRequest(**{"pr_url": _PR, "image_url": _IMAGE_URL, **fields})
    return _frame(root, "ReadPullRequestImage", request.SerializeToString(), vouch=_VOUCHED)


def record_image_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Each image the runner was asked for: its workspace, PR and URL; each answers _IMAGE."""
    asked: list[tuple[str, str, str]] = []

    def read(root: str, reference: PullRequestRef, image_url: str) -> PullRequestImage:
        asked.append((root, reference.url, image_url))
        return _IMAGE

    monkeypatch.setattr(github_images, "read_pull_request_image", read)
    return asked


@pytest.fixture
def image_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    return record_image_reads(monkeypatch)


async def test_an_image_streams_its_type_and_size_then_its_bytes_in_parts(
    tmp_path: Path, image_reads: list[tuple[str, str, str]]
) -> None:
    messages, code, message = await _answer(_image_frame(tmp_path))

    assert code is None, message
    assert image_reads == [(str(tmp_path.resolve()), _PR, _IMAGE_URL)]
    replies = [pb.ReadPullRequestImageResponse.FromString(message) for message in messages]
    assert replies[0].metadata == pb.PullRequestImageMetadata(
        content_type="image/png", size=len(_IMAGE.data)
    )
    assert not any(reply.HasField("metadata") for reply in replies[1:])
    part = 256 * 1024
    assert [len(reply.data) for reply in replies[1:]] == [part, part, len(_IMAGE.data) - 2 * part]
    assert b"".join(reply.data for reply in replies) == _IMAGE.data


async def test_an_image_of_a_pr_the_session_never_recorded_isnt_read(
    tmp_path: Path, image_reads: list[tuple[str, str, str]]
) -> None:
    request = pb.ReadPullRequestImageRequest(pr_url=_PR, image_url=_IMAGE_URL)
    _messages, code, _message = await _answer(
        _frame(tmp_path, "ReadPullRequestImage", request.SerializeToString())
    )

    assert code == Code.FAILED_PRECONDITION.value
    assert image_reads == []


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("host_not_allowed", Code.FAILED_PRECONDITION),
        ("too_large", Code.FAILED_PRECONDITION),
        ("not_image", Code.FAILED_PRECONDITION),
        ("fetch_failed", Code.UNAVAILABLE),
    ],
)
async def test_an_image_refusal_reaches_the_server_with_its_code_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, code: Code
) -> None:
    def refuse(_root: str, _reference: PullRequestRef, _image_url: str) -> PullRequestImage:
        raise ImageRefused(reason)

    monkeypatch.setattr(github_images, "read_pull_request_image", refuse)
    messages, error_code, message = await _answer(_image_frame(tmp_path))

    assert (messages, error_code) == ([], code.value)
    assert message is not None
    assert "https://" not in message
    assert await _reason(_image_frame(tmp_path)) == (code.value, reason)


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"image_url": ""}, "Expected an image URL"),
        (
            {"image_url": "https://github.com/" + "a" * 2030},
            "The image URL is longer than 2048 characters",
        ),
        ({"pr_url": _PR + "a" * 2048}, "The pull request URL is longer than 2048 characters"),
    ],
    ids=["no-url", "long-url", "long-pr-url"],
)
async def test_the_host_refuses_an_image_read_the_server_would_have(
    tmp_path: Path, image_reads: list[tuple[str, str, str]], fields: dict[str, str], refusal: str
) -> None:
    _messages, code, message = await _answer(_image_frame(tmp_path, **fields))

    assert (code, message) == (Code.INVALID_ARGUMENT.value, refusal)
    assert image_reads == []


@pytest.fixture
def account_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A config of its own for saved accounts; answers the gh commands a call ran with nothing."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(pr_resource, "pr_info", lambda _root, **_kwargs: {"object": "info"})
    commands: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: Any) -> tuple[int, str, str]:
        commands.append(argv)
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_gh", gh)
    return commands


def _saved_accounts() -> object:
    return config.load_global_config().get("github", {}).get("accounts", {})


async def _set_account(root: Path, **fields: str) -> tuple[str | None, str | None]:
    request = pb.SetPullRequestAccountRequest(**fields)
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)
    _messages, code, message = await _answer(
        _frame(root, "SetPullRequestAccount", request.SerializeToString(), vouch=vouch)
    )
    return code, message


async def test_a_pull_requests_account_is_saved_for_its_repository_alone(
    tmp_path: Path, account_store: list[list[str]]
) -> None:
    """The same key REST saves under; the workspace's own account isn't touched."""
    code, _ = await _set_account(tmp_path, pr_url=_PR, account="octocat")

    assert code is None
    assert _saved_accounts() == {"github.com/example/app": "octocat"}
    assert account_store == []


async def test_an_empty_account_keeps_the_saved_one(
    tmp_path: Path, account_store: list[list[str]]
) -> None:
    await _set_account(tmp_path, pr_url=_PR, account="octocat")
    code, _ = await _set_account(tmp_path, pr_url=_PR, account="")

    assert code is None
    assert _saved_accounts() == {"github.com/example/app": "octocat"}


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        # The server's vouch names a default PR; the method still won't change the workspace.
        ({"account": "octocat"}, "Expected a pull request URL"),
        (
            {"pr_url": _PR, "account": "octocat", "remote": "upstream"},
            "Changing the base repository isn't available here",
        ),
        (
            {"pr_url": _PR, "remote": "upstream"},
            "Changing the base repository isn't available here",
        ),
    ],
    ids=["no-pr", "remote-with-account", "remote-alone"],
)
async def test_set_account_changes_nothing_beyond_one_pull_request(
    tmp_path: Path, account_store: list[list[str]], fields: dict[str, str], refusal: str
) -> None:
    code, message = await _set_account(tmp_path, **fields)

    assert code == Code.INVALID_ARGUMENT.value
    assert message == refusal
    assert _saved_accounts() == {}
    assert account_store == []


@pytest.mark.parametrize("account", ["", "  "], ids=["empty", "blank"])
async def test_merge_as_an_account_needs_one(
    tmp_path: Path, account: str, owner_calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """Without one, the merge would run as the saved account instead of the confirmed one."""
    _record(_SESSION)
    request = pb.MergePullRequestAsAccountRequest(
        pr_url=_PR, merge_method="squash", expected_head_oid=_HEAD, account=account
    )
    _messages, code, message = await _answer(
        _frame(tmp_path, "MergePullRequestAsAccount", request.SerializeToString())
    )

    assert code == Code.INVALID_ARGUMENT.value
    assert message == "Expected an account to merge as"
    assert owner_calls == []


# Each read: a request, the reader method it runs, and the arguments that method gets.
_READ_CALLS: dict[str, tuple[Any, str, tuple[Any, ...], dict[str, Any]]] = {
    "GetPullRequest": (pb.GetPullRequestRequest(pr_url=_PR), "github_info", (_SESSION, _PR), {}),
    "ListPullRequestFiles": (
        pb.ListPullRequestFilesRequest(pr_url=_PR, head_sha=_HEAD, base_sha=_BASE),
        "github_changes",
        (_SESSION, _PR),
        {"head_sha": _HEAD, "base_sha": _BASE},
    ),
    "GetPullRequestDiff": (
        pb.GetPullRequestDiffRequest(pr_url=_PR, head_sha=_HEAD),
        "github_pr_diff",
        (_SESSION, _PR),
        {"head_sha": _HEAD, "base_sha": None},
    ),
    "GetPullRequestDiff.file": (
        pb.GetPullRequestDiffRequest(
            pr_url=_PR,
            head_sha=_HEAD,
            base_sha=_BASE,
            path="src/app.py",
            previous_path="src/old.py",
            base="main",
        ),
        "github_file_diff",
        ("main", "src/app.py"),
        {
            "session_id": _SESSION,
            "pr_url": _PR,
            "previous_path": "src/old.py",
            "head_sha": _HEAD,
            "base_sha": _BASE,
        },
    ),
    "ListPullRequestCommits": (
        pb.ListPullRequestCommitsRequest(pr_url=_PR, head_sha=_HEAD),
        "github_commits",
        (_SESSION, _PR),
        {"head_sha": _HEAD},
    ),
    "GetCommitDiff": (
        pb.GetCommitDiffRequest(pr_url=_PR, commit_sha=_HEAD),
        "github_commit_diff",
        (_SESSION, _PR),
        {"commit_sha": _HEAD},
    ),
    "ComparePullRequestRange": (
        pb.ComparePullRequestRangeRequest(pr_url=_PR, head_sha=_HEAD, from_sha=_BASE),
        "github_compare_diff",
        (_SESSION, _PR),
        {"head_sha": _HEAD, "from_sha": _BASE},
    ),
    "ListReviewThreads": (
        pb.ListReviewThreadsRequest(
            pr_url=_PR, page_token="cursor_1", head_sha=_HEAD, base_sha=_BASE
        ),
        "github_review_threads",
        (_SESSION, _PR, "cursor_1", _HEAD, _BASE),
        {"account_rights": False},
    ),
    "ListReviewThreadComments": (
        pb.ListReviewThreadCommentsRequest(
            pr_url=_PR, thread_id="thread_1", page_token="cursor_2"
        ),
        "github_review_thread_comments",
        (_SESSION, _PR, "thread_1", "cursor_2"),
        {"account_rights": False},
    ),
}


def test_every_read_has_an_argument_case() -> None:
    assert {case.split(".")[0] for case in _READ_CALLS} == _PULL_REQUEST_READS


@pytest.mark.parametrize("case", sorted(_READ_CALLS))
async def test_a_read_passes_the_requests_fields_to_its_reader_and_its_answer_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    request, reader_method, args, kwargs = _READ_CALLS[case]
    method = case.split(".")[0]
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    for name in {reader for _request, reader, _args, _kwargs in _READ_CALLS.values()}:

        def record(_self: Any, *got: Any, _name: str = name, **got_kwargs: Any) -> dict[str, Any]:
            calls.append((_name, got, got_kwargs))
            return {"object": "test", "count": 3, "items": [{"line": 4}]}

        monkeypatch.setattr(WorkspaceReader, name, record)
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)

    messages, code, message = await _answer(
        _frame(tmp_path, method, request.SerializeToString(), vouch=vouch)
    )

    assert code is None, message
    assert calls == [(reader_method, args, kwargs)]
    payload = payload_dict(WORKSPACE_METHODS[method].response_type.FromString(messages[0]).payload)
    assert payload == {"object": "test", "count": 3, "items": [{"line": 4}]}


# A review page as GitHub answers it for the host's account, which wrote and reacted.
_ACCOUNT_COMMENT = {
    "id": "PRRC_1",
    "database_id": 5,
    "author": "host-login",
    "body": "Why this?",
    "reactions": [{"content": "HEART", "count": 2, "viewer_reacted": True}],
    "can_react": True,
    "can_update": True,
    "can_delete": True,
    "viewer_did_author": True,
}
_ACCOUNT_THREADS = {
    "data": [
        {
            "id": "PRRT_1",
            "can_reply": True,
            "can_resolve": True,
            "can_unresolve": False,
            "comments": {"data": [_ACCOUNT_COMMENT], "has_more": False},
        }
    ],
    "has_more": False,
}


class _AccountFacet:
    """A provider that answers every review read with the host account's rights."""

    def review_threads(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(_ACCOUNT_THREADS)

    def review_thread_comments(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(_ACCOUNT_THREADS["data"][0]["comments"])


@pytest.mark.parametrize("method", ["ListReviewThreads", "ListReviewThreadComments"])
@pytest.mark.parametrize("account_rights", [False, True], ids=["older-server", "owner-asks"])
async def test_a_review_read_carries_the_accounts_rights_only_when_the_server_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, account_rights: bool
) -> None:
    """An older server shows the host's page to anyone, so it gets none of the account's."""
    monkeypatch.setattr(pr_resource, "_review_target", lambda *_a, **_k: (_AccountFacet(), None))
    request = (
        pb.ListReviewThreadsRequest(pr_url=_PR)
        if method == "ListReviewThreads"
        else pb.ListReviewThreadCommentsRequest(pr_url=_PR, thread_id="PRRT_1", page_token="c")
    )
    vouch = PullRequestVouch(server_tracked=True)

    messages, code, message = await _answer(
        _frame(
            tmp_path,
            method,
            request.SerializeToString(),
            vouch=vouch,
            account_rights=account_rights,
        )
    )

    assert code is None, message
    page = payload_dict(WORKSPACE_METHODS[method].response_type.FromString(messages[0]).payload)
    if method == "ListReviewThreads":
        [thread] = page["data"]
        assert (thread["id"], thread["can_reply"]) == ("PRRT_1", True)
        assert ("can_resolve" in thread, "can_unresolve" in thread) == (account_rights,) * 2
        [comment] = thread["comments"]["data"]
    else:
        [comment] = page["data"]
    assert (comment["id"], comment["database_id"], comment["author"]) == (
        "PRRC_1",
        5,
        "host-login",
    )
    assert comment["reactions"][0]["count"] == 2
    if account_rights:
        assert comment == _ACCOUNT_COMMENT
    else:
        assert not {"can_react", "can_update", "can_delete", "viewer_did_author"} & comment.keys()
        assert comment["reactions"] == [{"content": "HEART", "count": 2}]


@pytest.mark.parametrize("case", ["ListReviewThreads", "ListReviewThreadComments"])
async def test_the_servers_account_rights_reach_the_review_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    request, reader_method, args, _kwargs = _READ_CALLS[case]
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def record(_self: Any, *got: Any, **got_kwargs: Any) -> dict[str, Any]:
        calls.append((reader_method, got, got_kwargs))
        return {"data": []}

    monkeypatch.setattr(WorkspaceReader, reader_method, record)
    vouch = PullRequestVouch(server_tracked=True, default_pr_url=_PR)

    _messages, code, message = await _answer(
        _frame(tmp_path, case, request.SerializeToString(), vouch=vouch, account_rights=True)
    )

    assert code is None, message
    assert calls == [(reader_method, args, {"account_rights": True})]
