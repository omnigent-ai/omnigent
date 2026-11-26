"""The server's route for the pull request reads, end to end through the host tunnel.

The server sends the authorized session and its vouch from the PRs it stores; the
host reads only what that allows. Each case counts the calls that reach the host and
the PR reads that get past the host's scope check. The callers' gates are in
``tests/server/routes/test_shell_permission_gate.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.protocol import ProtocolType

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host import workspace_service
from omnigent.host.frames import (
    HostHelloFrame,
    HostWorkspaceCallFrame,
    HostWorkspaceReplyFrame,
    PullRequestVouch,
    decode_host_frame,
)
from omnigent.host.workspace_methods import WORKSPACE_METHODS
from omnigent.host.workspace_payloads import payload_dict
from omnigent.runner import pr_resource
from omnigent.server import session_pull_requests as server_prs
from omnigent.server import workspace_access, workspace_api
from omnigent.server.host_registry import HostConnection, HostRegistry, deliver_workspace_reply
from omnigent.server.workspace_access import METHOD_GATES, WorkspaceGate, callable_methods
from omnigent.stores.session_pull_request_store import SessionPullRequests
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.polling import poll_until_async
from tests.host import test_workspace_pull_requests as host_tests
from tests.host import test_workspace_service as service_tests
from tests.host.test_workspace_methods import (
    _PULL_REQUEST_OWNER_METHODS,
    _PULL_REQUEST_READS,
    _PULL_REQUEST_WRITES,
)

_SESSION = service_tests._SESSION
_PR = "https://github.com/example/app/pull/7"
_OTHER_PR = "https://github.com/example/app/pull/8"


@dataclass
class _Entry:
    url: str


class _Store:
    """The server's PR store, reduced to what the vouch reads."""

    def __init__(self, stored: SessionPullRequests | None = None, *, fails: bool = False) -> None:
        self.stored = stored
        self.fails = fails
        self.reads = 0

    def read(self, _session_id: str) -> SessionPullRequests | None:
        self.reads += 1
        if self.fails:
            raise RuntimeError("storage is down")
        return self.stored


def _linked(*urls: str, synced: bool = True) -> SessionPullRequests:
    return SessionPullRequests(entries=[_Entry(url) for url in urls], synced=synced)  # type: ignore[misc]


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def host_calls(monkeypatch: pytest.MonkeyPatch) -> list[HostWorkspaceCallFrame]:
    """Every call frame that reached the host."""
    calls: list[HostWorkspaceCallFrame] = []
    real = service_tests.serve_tunnel_call

    def counting(reader_for: Any, frame: HostWorkspaceCallFrame) -> AsyncIterator[Any]:
        calls.append(frame)
        return real(reader_for, frame)

    monkeypatch.setattr(service_tests, "serve_tunnel_call", counting)
    return calls


@pytest.fixture
def provider_reads(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The PRs a read got past the host's scope check for; each answers with no files."""
    reached: list[str | None] = []

    def serving_facet(_root: str, reference: Any) -> None:
        reached.append(reference.url if reference is not None else None)

    monkeypatch.setattr(pr_resource, "_serving_facet", serving_facet)
    return reached


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: _Store | None) -> Any:
    monkeypatch.setattr(server_prs, "open_store", lambda _conversations: store)
    app = service_tests._server_app(tmp_path, monkeypatch)
    return service_tests._connect(app, service_tests._SERVER_BASE, ProtocolType.CONNECT)


def test_every_method_has_a_gate_and_the_pull_request_reads_keep_their_rest_gates() -> None:
    assert set(METHOD_GATES) == set(WORKSPACE_METHODS)
    assert {name: METHOD_GATES[name] for name in _PULL_REQUEST_OWNER_METHODS} == dict.fromkeys(
        _PULL_REQUEST_OWNER_METHODS, WorkspaceGate.SESSION_OWNER
    )
    assert {name: METHOD_GATES[name] for name in _PULL_REQUEST_READS} == {
        "GetPullRequest": WorkspaceGate.SESSION_OWNER,
        "ListPullRequestFiles": WorkspaceGate.BROWSE,
        "GetPullRequestDiff": WorkspaceGate.BROWSE,
        "ListPullRequestCommits": WorkspaceGate.BROWSE,
        "GetCommitDiff": WorkspaceGate.BROWSE,
        "ComparePullRequestRange": WorkspaceGate.BROWSE,
        "ListReviewThreads": WorkspaceGate.BROWSE,
        "ListReviewThreadComments": WorkspaceGate.READ,
    }


@pytest.mark.parametrize("browse", [True, False], ids=["browse", "read-only"])
def test_only_the_owner_is_offered_the_owner_methods(browse: bool) -> None:
    """A non-owner's GetCapabilities leaves them out, so their client offers no edit controls."""
    others = callable_methods(browse=browse, session_owner=False)
    owner = callable_methods(browse=browse, session_owner=True)

    assert not others & (_PULL_REQUEST_OWNER_METHODS | {"GetPullRequest"})
    assert owner - others == _PULL_REQUEST_OWNER_METHODS | {"GetPullRequest"}


async def test_after_import_an_unlinked_pr_is_refused_before_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    provider_reads: list[str | None],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    with pytest.raises(ConnectError) as refused:
        await client.list_pull_request_files(pb.ListPullRequestFilesRequest(pr_url=_OTHER_PR))

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert refused.value.message == "This pull request is not associated with the session"
    assert host_calls == []
    assert provider_reads == []


async def test_after_import_a_linked_pr_is_read_on_the_servers_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    provider_reads: list[str | None],
) -> None:
    """The host never recorded the PR; the server's vouch is what lets it read."""
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    await client.list_pull_request_files(pb.ListPullRequestFilesRequest(pr_url=_PR))
    await client.list_pull_request_files(pb.ListPullRequestFilesRequest())

    assert [(call.session_id, call.vouch) for call in host_calls] == [
        (_SESSION, PullRequestVouch(server_tracked=True)),
        (_SESSION, PullRequestVouch(server_tracked=True, default_pr_url=_PR)),
    ]
    assert provider_reads == [_PR, _PR]


@pytest.mark.parametrize(
    "store",
    [_Store(_linked(_PR, synced=False)), _Store(fails=True), None],
    ids=["not-imported", "store-read-fails", "no-store"],
)
async def test_before_import_the_host_refuses_a_pr_it_never_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: _Store | None,
    host_calls: list[HostWorkspaceCallFrame],
    provider_reads: list[str | None],
) -> None:
    """Without the server's word, the host's own record for the session decides."""
    client = _client(tmp_path, monkeypatch, store)

    with pytest.raises(ConnectError) as refused:
        await client.list_pull_request_files(pb.ListPullRequestFilesRequest(pr_url=_PR))

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert [(call.session_id, call.vouch) for call in host_calls] == [(_SESSION, None)]
    assert provider_reads == []


async def test_the_session_sent_to_the_host_is_the_one_in_the_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    provider_reads: list[str | None],
) -> None:
    """Neither a header nor an extra request field can name another session or vouch."""
    monkeypatch.setattr(server_prs, "open_store", lambda _conversations: None)
    app = service_tests._server_app(tmp_path, monkeypatch)
    crafted = pb.ListPullRequestFilesRequest(pr_url=_PR).SerializeToString() + b"\x78\x01"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as http:
        response = await http.post(
            f"/v1/sessions/{_SESSION}/workspace"
            "/omnigent.api.workspace.v1alpha1.WorkspaceService/ListPullRequestFiles",
            content=crafted,
            headers={
                "Content-Type": "application/proto",
                "X-Omnigent-Session": "conv_other",
                "X-Omnigent-Vouch": '{"server_tracked": true}',
            },
        )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "failed_precondition"
    assert [(call.session_id, call.vouch) for call in host_calls] == [(_SESSION, None)]
    assert provider_reads == []


async def test_capabilities_through_the_server_list_the_pull_request_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    client = _client(tmp_path, monkeypatch, None)

    response = await client.get_capabilities(pb.GetCapabilitiesRequest())

    # Permissions are off here, so the caller is the session's owner.
    assert set(response.capabilities) >= _PULL_REQUEST_READS | _PULL_REQUEST_OWNER_METHODS
    assert [(call.session_id, call.vouch) for call in host_calls] == [(_SESSION, None)]


_CREATE_COMMENT = host_tests._OWNER_CALLS["CreateComment"][0]


async def test_a_change_to_an_unlinked_pr_is_refused_before_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_OTHER_PR)))

    with pytest.raises(ConnectError) as refused:
        await client.create_comment(_CREATE_COMMENT)

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert host_calls == []
    assert owner_calls == []


async def test_a_change_to_a_linked_pr_runs_on_the_servers_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """The host never recorded the PR; the server's vouch is what lets it change."""
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    response = await client.create_comment(_CREATE_COMMENT)

    assert payload_dict(response.payload) == {"object": "test"}
    assert [(call.session_id, call.vouch) for call in host_calls] == [
        (_SESSION, PullRequestVouch(server_tracked=True)),
    ]
    assert [call[0] for call in owner_calls] == ["create_pr_comment"]


@pytest.fixture
def owner_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Each pr_resource call an owner method made on the host."""
    return host_tests.record_owner_calls(monkeypatch)


@pytest.mark.parametrize(
    ("method", "request_message", "refusal"),
    [
        (
            "SetPullRequestAccount",
            pb.SetPullRequestAccountRequest(account="octocat"),
            "Expected a pull request URL",
        ),
        (
            "SetPullRequestAccount",
            pb.SetPullRequestAccountRequest(pr_url=_PR, remote="upstream"),
            "Changing the base repository isn't available here",
        ),
        (
            "MergePullRequestAsAccount",
            pb.MergePullRequestAsAccountRequest(
                pr_url=_PR, merge_method="squash", expected_head_oid="a" * 40, account=" "
            ),
            "Expected an account to merge as",
        ),
        (
            "ListLabels",
            pb.ListLabelsRequest(pr_url=_PR, text="bug"),
            "Label search isn't available on this host",
        ),
    ],
    ids=["account-without-pr", "account-with-remote", "merge-as-no-account", "label-search"],
)
async def test_a_request_the_api_doesnt_serve_is_refused_before_the_vouch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    method: str,
    request_message: Any,
    refusal: str,
) -> None:
    store = _Store(_linked(_PR))
    client = _client(tmp_path, monkeypatch, store)

    with pytest.raises(ConnectError) as refused:
        await getattr(client, WORKSPACE_METHODS[method].python_name)(request_message)

    assert (refused.value.code, refused.value.message) == (Code.INVALID_ARGUMENT, refusal)
    assert store.reads == 0
    assert host_calls == []


_OVERVIEW = {"pr": None, "prs": [{"url": _PR}], "count": 2}


def _get_answers_the_overview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        WorkspaceReader, "github_info", lambda _self, _session, _url: dict(_OVERVIEW)
    )


def _set_account_answers_the_overview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_resource, "set_pr_preference", lambda _root, **_kwargs: dict(_OVERVIEW))
    monkeypatch.setattr(pr_resource, "create_pr_comment", lambda _root, **_kwargs: {"id": 1})


@pytest.mark.parametrize(
    ("answer_overview", "method", "request_message", "then", "then_request"),
    [
        pytest.param(
            _get_answers_the_overview,
            "GetPullRequest",
            pb.GetPullRequestRequest(pr_url=_PR),
            "ListPullRequestFiles",
            pb.ListPullRequestFilesRequest(pr_url=_PR),
            id="get-pull-request",
        ),
        pytest.param(
            _set_account_answers_the_overview,
            "SetPullRequestAccount",
            pb.SetPullRequestAccountRequest(pr_url=_PR, account="octocat"),
            "CreateComment",
            _CREATE_COMMENT,
            id="set-account",
        ),
    ],
)
async def test_an_overview_answer_stores_the_reported_prs_and_answers_with_the_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    provider_reads: list[str | None],
    answer_overview: Callable[[pytest.MonkeyPatch], None],
    method: str,
    request_message: Any,
    then: str,
    then_request: Any,
) -> None:
    """These answers are the PR overview, so they update the server's list as REST does."""
    stored = _linked(_PR)
    ingested: list[dict[str, Any]] = []

    def ingest(_store: object, session_id: str, info: Any, *, requested_at: float) -> Any:
        assert session_id == _SESSION and requested_at > 0
        ingested.append(dict(info))
        return stored

    monkeypatch.setattr(server_prs, "ingest_info", ingest)
    monkeypatch.setattr(server_prs, "answer", lambda info, _stored: {**info, "prs": ["server"]})
    answer_overview(monkeypatch)
    client = _client(tmp_path, monkeypatch, _Store(stored))

    def send(name: str) -> Any:
        return getattr(client, WORKSPACE_METHODS[name].python_name)

    response = await send(method)(request_message)
    await send(then)(then_request)

    assert payload_dict(response.payload) == {**_OVERVIEW, "prs": ["server"]}
    # The ordinary call after it stores nothing.
    assert ingested == [_OVERVIEW]
    # The Struct carries numbers as floats; what's stored gets the integer back.
    assert type(ingested[0]["count"]) is int
    assert [call.method for call in host_calls] == [method, then]


_SERVICE_PATH = (
    f"/v1/sessions/{_SESSION}/workspace/omnigent.api.workspace.v1alpha1.WorkspaceService"
)


async def _post(app: Any, method: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as http:
        return await http.post(f"{_SERVICE_PATH}/{method}", content=content, headers=headers)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "application/json", "Origin": "https://attacker.example"}, 403),
        # A body a page on another site can post without asking first.
        ({"Content-Type": "text/plain"}, 415),
    ],
    ids=["untrusted-origin", "simple-request"],
)
async def test_a_change_from_another_site_is_refused_before_any_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    headers: dict[str, str],
    status: int,
) -> None:
    store = _Store(_linked(_PR))
    app = service_tests._server_app(tmp_path, monkeypatch)
    monkeypatch.setattr(server_prs, "open_store", lambda _conversations: store)
    body = b'{"prUrl": "https://github.com/example/app/pull/7", "body": "Looks good"}'

    response = await _post(app, "CreateComment", body, headers)

    assert response.status_code == status, response.text
    assert store.reads == 0
    assert host_calls == []


async def test_a_change_is_never_served_over_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    app = service_tests._server_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as http:
        response = await http.get(f"{_SERVICE_PATH}/CreateComment", params={"message": "{}"})

    assert response.status_code == 405, response.text
    assert host_calls == []


# -- A change whose answer is lost is never reported as safe to retry. --------------------


async def _no_vouch(_pr_url: str) -> PullRequestVouch | None:
    return None


async def _as_reported(payload: dict[str, Any], _sent_at: float) -> dict[str, Any]:
    return payload


@dataclass
class _Tunnel:
    """A connected host whose frames the test answers, or drops."""

    registry: HostRegistry
    host: HostConnection
    service: workspace_api.HostWorkspaceService
    sent: list[HostWorkspaceCallFrame]

    async def call(self, method: str, request: Any) -> Any:
        return await getattr(self.service, WORKSPACE_METHODS[method].python_name)(request, None)

    async def frame_sent(self) -> None:
        await poll_until_async(lambda: self.host.pending_workspace_calls, timeout_s=5)


def _tunnel(tmp_path: Path, *, refuse_send: bool = False) -> _Tunnel:
    registry = HostRegistry()
    hello = HostHelloFrame(version="t", frame_protocol_version=1, name="ws")
    host = registry.register("host_ws", object(), hello, owner=None)  # type: ignore[arg-type]
    sent: list[HostWorkspaceCallFrame] = []

    def send_text(_conn: HostConnection, text: str) -> None:
        if refuse_send:
            raise ConnectionError("closed")
        frame = decode_host_frame(text)
        assert isinstance(frame, HostWorkspaceCallFrame)
        sent.append(frame)

    registry.send_text = send_text  # type: ignore[method-assign]
    service = workspace_api.HostWorkspaceService(
        workspace_api.HostTarget(registry, host, str(tmp_path)),
        content_search=False,
        pull_requests=workspace_api.PullRequestRoute(
            _SESSION, vouch=_no_vouch, overview=_as_reported
        ),
    )
    return _Tunnel(registry, host, service, sent)


_MERGE = host_tests._OWNER_CALLS["MergePullRequest"][0]
_COMMENT_UNCONFIRMED = "Could not confirm comment posting. Check GitHub before retrying."
_CHANGE_UNCONFIRMED = "Could not confirm the pull request change. Check GitHub before retrying."
_COMMENT_METHODS = {"CreateComment", "CreateReviewThread", "ReplyToReviewThread"}


@pytest.mark.parametrize(
    ("method", "request_message", "unconfirmed"),
    [
        ("CreateComment", _CREATE_COMMENT, _COMMENT_UNCONFIRMED),
        ("MergePullRequest", _MERGE, _CHANGE_UNCONFIRMED),
    ],
    ids=["comment", "merge"],
)
async def test_a_host_that_drops_after_the_frame_leaves_the_change_unconfirmed(
    tmp_path: Path, method: str, request_message: Any, unconfirmed: str
) -> None:
    tunnel = _tunnel(tmp_path)
    call = asyncio.create_task(tunnel.call(method, request_message))
    await tunnel.frame_sent()
    tunnel.registry.deregister("host_ws", conn=tunnel.host)

    with pytest.raises(ConnectError) as lost:
        await call

    assert (lost.value.code, lost.value.message) == (Code.UNKNOWN, unconfirmed)
    assert [frame.method for frame in tunnel.sent] == [method]


async def test_an_unavailable_answer_after_the_frame_leaves_the_change_unconfirmed(
    tmp_path: Path,
) -> None:
    """Whoever reports UNAVAILABLE once the frame is out, the change may have been made."""
    tunnel = _tunnel(tmp_path)
    call = asyncio.create_task(tunnel.call("MergePullRequest", _MERGE))
    await tunnel.frame_sent()
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=tunnel.sent[0].request_id,
            done=True,
            error_code="unavailable",
            error_message="GitHub is unreachable",
        ),
    )

    with pytest.raises(ConnectError) as lost:
        await call

    assert (lost.value.code, lost.value.message) == (Code.UNKNOWN, _CHANGE_UNCONFIRMED)


_WRITES = sorted(_PULL_REQUEST_WRITES)


async def _end_after_frame(tunnel: _Tunnel, method: str, request: Any, code: str) -> ConnectError:
    """The error a call gets when the host ends it with ``code`` after the frame went out."""
    call = asyncio.create_task(tunnel.call(method, request))
    await tunnel.frame_sent()
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=tunnel.sent[-1].request_id,
            done=True,
            error_code=code,
            error_message="Workspace call failed",
        ),
    )
    with pytest.raises(ConnectError) as ended:
        await call
    return ended.value


def test_every_write_has_a_request_case() -> None:
    assert set(_WRITES) <= set(host_tests._OWNER_CALLS)
    assert len(_WRITES) == 10


@pytest.mark.parametrize("method", _WRITES)
async def test_a_host_failure_after_a_change_was_sent_is_unconfirmed(
    tmp_path: Path, method: str
) -> None:
    """A host error shaping GitHub's answer may follow a change GitHub made."""
    unconfirmed = _COMMENT_UNCONFIRMED if method in _COMMENT_METHODS else _CHANGE_UNCONFIRMED
    tunnel = _tunnel(tmp_path)
    error = await _end_after_frame(tunnel, method, host_tests._OWNER_CALLS[method][0], "internal")

    assert (error.code, error.message) == (Code.UNKNOWN, unconfirmed)


@pytest.mark.parametrize("method", ["GetMergeOptions", "ListPullRequestFiles"])
async def test_a_host_failure_on_a_read_stays_internal(tmp_path: Path, method: str) -> None:
    tunnel = _tunnel(tmp_path)
    request = WORKSPACE_METHODS[method].request_type(pr_url=_PR)
    error = await _end_after_frame(tunnel, method, request, "internal")

    assert (error.code, error.message) == (Code.INTERNAL, "Workspace call failed")


async def test_a_change_whose_answer_times_out_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_api, "_WRITE_REPLY_TIMEOUT_S", 0.05)
    tunnel = _tunnel(tmp_path)

    with pytest.raises(ConnectError) as lost:
        await tunnel.call("CreateComment", _CREATE_COMMENT)

    assert (lost.value.code, lost.value.message) == (Code.DEADLINE_EXCEEDED, _COMMENT_UNCONFIRMED)
    assert len(tunnel.sent) == 1


async def test_a_change_that_was_never_sent_is_safe_to_retry(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path, refuse_send=True)

    with pytest.raises(ConnectError) as unsent:
        await tunnel.call("CreateComment", _CREATE_COMMENT)

    assert unsent.value.code == Code.UNAVAILABLE
    assert tunnel.sent == []


@pytest.mark.parametrize("method", ["ListPullRequestFiles", "GetMergeOptions"])
async def test_a_read_cut_off_by_the_host_stays_safe_to_retry(tmp_path: Path, method: str) -> None:
    tunnel = _tunnel(tmp_path)
    request = WORKSPACE_METHODS[method].request_type(pr_url=_PR)
    call = asyncio.create_task(tunnel.call(method, request))
    await tunnel.frame_sent()
    tunnel.registry.deregister("host_ws", conn=tunnel.host)

    with pytest.raises(ConnectError) as cut_off:
        await call

    assert cut_off.value.code == Code.UNAVAILABLE


def _listing_through_server(tmp_path: Path, host_lists: list[str], *, session_owner: bool) -> Any:
    """The server's GetCapabilities, for a host that lists ``host_lists``."""
    registry = HostRegistry()
    hello = HostHelloFrame(version="t", frame_protocol_version=1, name="ws")
    host = registry.register("host_ws", object(), hello, owner=None)  # type: ignore[arg-type]

    def send_text(conn: HostConnection, text: str) -> None:
        call = decode_host_frame(text)
        assert isinstance(call, HostWorkspaceCallFrame)
        listed = pb.GetCapabilitiesResponse(capabilities=host_lists).SerializeToString()
        deliver_workspace_reply(
            conn, HostWorkspaceReplyFrame(request_id=call.request_id, message=listed, done=True)
        )

    registry.send_text = send_text  # type: ignore[method-assign]
    service = workspace_api.HostWorkspaceService(
        workspace_api.HostTarget(registry, host, str(tmp_path)),
        content_search=False,
        pull_requests=workspace_api.PullRequestRoute(
            _SESSION, vouch=_no_vouch, overview=_as_reported
        ),
        callable_methods=callable_methods(browse=True, session_owner=session_owner),
    )
    return service.get_capabilities(pb.GetCapabilitiesRequest(), None)  # type: ignore[arg-type]


@pytest.mark.parametrize("session_owner", [True, False], ids=["owner", "non-owner"])
async def test_a_newer_hosts_methods_this_server_doesnt_serve_arent_listed(
    tmp_path: Path, session_owner: bool
) -> None:
    """A client offers what GetCapabilities lists, and this server would answer 404 for it."""
    host_lists = [
        "ListDirectory",
        "list_directory.collapse_single_child_directories",
        "GetPullRequest",
        "ListPullRequestFiles",
        "ArchivePullRequest",
        "archive_pull_request.undo",
    ]

    response = await _listing_through_server(tmp_path, host_lists, session_owner=session_owner)

    # A feature passes on; the client checks it only with a method it was offered.
    expected = [
        "ListDirectory",
        "list_directory.collapse_single_child_directories",
        *(["GetPullRequest"] if session_owner else []),
        "ListPullRequestFiles",
        "archive_pull_request.undo",
    ]
    assert list(response.capabilities) == expected


@pytest.fixture
def owner_lookups(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Each owner lookup the route made, and whether it ran on the event loop's thread."""
    import threading

    from omnigent.server.routes.sessions import routes_resources

    lookups: list[bool] = []
    real = routes_resources._is_session_owner
    loop_thread = threading.current_thread()

    def recording(*args: Any, **kwargs: Any) -> bool:
        lookups.append(threading.current_thread() is loop_thread)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes_resources, "_is_session_owner", recording)
    return lookups


async def test_only_owner_gated_calls_look_up_the_owner_and_never_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_lookups: list[bool]
) -> None:
    """With permissions on, the lookup reads the database."""
    monkeypatch.setattr(
        WorkspaceReader, "github_info", lambda _self, _session, _url: {"pr": None, "prs": []}
    )
    client = _client(tmp_path, monkeypatch, None)

    await client.list_directory(pb.ListDirectoryRequest())
    # A PR the session never linked; the host refuses it, after the gate.
    with pytest.raises(ConnectError):
        await client.list_pull_request_files(pb.ListPullRequestFilesRequest(pr_url=_PR))
    assert owner_lookups == []
    await client.get_capabilities(pb.GetCapabilitiesRequest())
    await client.get_pull_request(pb.GetPullRequestRequest())

    assert owner_lookups == [False, False]


# -- Mixed versions: each side offers only what the other can serve. ----------------------


@pytest.fixture
def reads_only_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A simulated server without the owner methods: no table entry and no gate for them.

    It keeps this server's capability filtering, so it stands in for a server whose
    method table lacks them, not for any one released version.
    """
    monkeypatch.setattr(
        workspace_api,
        "WORKSPACE_METHODS",
        {
            name: method
            for name, method in WORKSPACE_METHODS.items()
            if name not in _PULL_REQUEST_OWNER_METHODS
        },
    )
    monkeypatch.setattr(
        workspace_access,
        "METHOD_GATES",
        {
            name: gate
            for name, gate in METHOD_GATES.items()
            if name not in _PULL_REQUEST_OWNER_METHODS
        },
    )


@pytest.fixture
def reads_only_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host as it was before the owner methods: it neither lists nor serves them."""
    for name in _PULL_REQUEST_OWNER_METHODS:
        monkeypatch.delitem(workspace_service._SERVED, name)
        monkeypatch.delattr(
            workspace_service.WorkspaceService, WORKSPACE_METHODS[name].python_name
        )


async def test_a_new_host_behind_a_reads_only_server_offers_no_owner_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reads_only_server: None,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """The host lists them, but this server can't forward them, so the owner isn't offered them."""
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    listed = await client.get_capabilities(pb.GetCapabilitiesRequest())
    with pytest.raises(ConnectError) as refused:
        await client.create_comment(_CREATE_COMMENT)

    assert set(listed.capabilities) >= _PULL_REQUEST_READS
    assert not set(listed.capabilities) & _PULL_REQUEST_OWNER_METHODS
    # The route's 404 for a method it has no gate for.
    assert refused.value.code == Code.UNIMPLEMENTED
    assert [call.method for call in host_calls] == ["GetCapabilities"]
    assert owner_calls == []


async def test_an_old_host_behind_the_new_server_offers_no_owner_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reads_only_host: None,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """A call made anyway gets the host's UNIMPLEMENTED, which says nothing was changed."""
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    listed = await client.get_capabilities(pb.GetCapabilitiesRequest())
    with pytest.raises(ConnectError) as refused:
        await client.create_comment(_CREATE_COMMENT)

    assert set(listed.capabilities) >= _PULL_REQUEST_READS
    assert not set(listed.capabilities) & _PULL_REQUEST_OWNER_METHODS
    assert refused.value.code == Code.UNIMPLEMENTED
    assert [call.method for call in host_calls] == ["GetCapabilities", "CreateComment"]
    assert owner_calls == []


@pytest.mark.parametrize("session_owner", [True, False], ids=["owner", "non-owner"])
async def test_a_new_hosts_owner_methods_are_offered_to_the_owner_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_owner: bool
) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    host_lists = workspace_service.capabilities(pull_requests=True)

    response = await _listing_through_server(tmp_path, host_lists, session_owner=session_owner)

    listed = set(response.capabilities)
    owner_only = _PULL_REQUEST_OWNER_METHODS | {"GetPullRequest"}
    assert listed >= _PULL_REQUEST_READS - owner_only
    assert (listed & owner_only) == (owner_only if session_owner else set())
