"""The host's pull request reads: served only over the tunnel, for the session's own PRs.

Every case runs through :func:`serve_tunnel_call` or the direct socket, the two ways a
call reaches the host, so the scope and vouch checks are tested where calls arrive.
A provider read past the scope check is a test failure unless the case allows it.
"""

from __future__ import annotations

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
from omnigent.runner import github_resource, pr_resource
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.workspace_fs import WorkspaceReader
from tests.host.test_workspace_methods import _PULL_REQUEST_OWNER_METHODS, _PULL_REQUEST_READS

_SESSION = "conv_prs"
_PR = "https://github.com/example/app/pull/7"
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
) -> HostWorkspaceCallFrame:
    return HostWorkspaceCallFrame("r1", str(root), method, request, session_id, vouch)


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
                request = WORKSPACE_METHODS[method].request_type(pr_url=_PR)
                binary = request.SerializeToString() + b"\x78\x01"
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


def _pr_target(**fields: Any) -> dict[str, Any]:
    return {"session_id": _SESSION, "pr_url": _PR, **fields}


# Each owner method: a valid request, the pr_resource function it runs, and its arguments.
_OWNER_CALLS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "ListLabels": (pb.ListLabelsRequest(pr_url=_PR), "pr_labels", _pr_target()),
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
}


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
    assert set(_OWNER_CALLS) == _PULL_REQUEST_OWNER_METHODS


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


async def test_list_labels_refuses_a_search_it_cant_do(
    tmp_path: Path, owner_calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    request = pb.ListLabelsRequest(pr_url=_PR, text="bug")
    _messages, code, message = await _answer(
        _frame(tmp_path, "ListLabels", request.SerializeToString())
    )

    assert code == Code.INVALID_ARGUMENT.value
    assert message == "Label search isn't available on this host"
    assert owner_calls == []


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
        {},
    ),
    "ListReviewThreadComments": (
        pb.ListReviewThreadCommentsRequest(
            pr_url=_PR, thread_id="thread_1", page_token="cursor_2"
        ),
        "github_review_thread_comments",
        (_SESSION, _PR, "thread_1", "cursor_2"),
        {},
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
