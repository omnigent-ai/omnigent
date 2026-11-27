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
from connectrpc.server import DEFAULT_READ_MAX_BYTES

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host import workspace_service
from omnigent.host.frames import (
    HostHelloFrame,
    HostWorkspaceCallFrame,
    HostWorkspaceReplyFrame,
    PullRequestVouch,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.workspace_methods import WORKSPACE_METHODS
from omnigent.host.workspace_payloads import payload_dict, payload_struct
from omnigent.host.workspace_pull_request_input import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_NAME_CHARACTERS,
    MAX_PULL_REQUEST_URL_CHARACTERS,
)
from omnigent.runner import github_images, pr_resource
from omnigent.server import session_pull_requests as server_prs
from omnigent.server import workspace_access, workspace_api
from omnigent.server.host_registry import HostConnection, HostRegistry, deliver_workspace_reply
from omnigent.server.pull_request_viewers import PullRequestViewer
from omnigent.server.read_only_code_hosts import READ_ONLY_CODE_HOST_MESSAGE
from omnigent.server.workspace_access import METHOD_GATES, WorkspaceGate, callable_methods
from omnigent.stores.session_pull_request_store import SessionPullRequests
from omnigent.util.tunnel_limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES
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


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: _Store | None,
    *,
    read_only_code_hosts: tuple[str, ...] = (),
) -> Any:
    monkeypatch.setattr(server_prs, "open_store", lambda _conversations: store)
    app = service_tests._server_app(
        tmp_path, monkeypatch, read_only_code_hosts=read_only_code_hosts
    )
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
            pb.ListLabelsRequest(pr_url=_PR, text="a" * 257),
            "The label search is longer than 256 characters",
        ),
        (
            "UploadAttachment",
            pb.UploadAttachmentRequest(pr_url=_PR, name=" ", content=b"png"),
            "Expected the file's name",
        ),
        (
            "UploadAttachment",
            pb.UploadAttachmentRequest(pr_url=_PR, name="a" * 256, content=b"png"),
            "The file's name is longer than 255 characters",
        ),
        (
            "UploadAttachment",
            pb.UploadAttachmentRequest(pr_url=_PR + "x" * 2048, name="shot.png", content=b"png"),
            "The pull request URL is longer than 2048 characters",
        ),
        (
            "UploadAttachment",
            pb.UploadAttachmentRequest(pr_url=_PR, name="shot.png"),
            "The attachment is empty",
        ),
    ],
    ids=[
        "account-without-pr",
        "account-with-remote",
        "merge-as-no-account",
        "label-search-too-long",
        "upload-no-name",
        "upload-long-name",
        "upload-long-url",
        "upload-empty",
    ],
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


# -- An upload reads past connectrpc's 4 MB default only for the session's owner. ---------

_OVER_DEFAULT_READ = 5 * 1024 * 1024


@pytest.fixture
def read_limits(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """The read limit of each Connect app the route built, one per call that passed its gates."""
    from omnigent.api.workspace.v1alpha1 import workspace_connect

    built: list[int | None] = []
    real = workspace_connect.WorkspaceServiceASGIApplication

    def recording(service: Any, *, read_max_bytes: int | None) -> Any:
        built.append(read_max_bytes)
        return real(service, read_max_bytes=read_max_bytes)

    monkeypatch.setattr(workspace_connect, "WorkspaceServiceASGIApplication", recording)
    return built


async def test_an_owners_upload_larger_than_the_default_read_limit_reaches_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
    read_limits: list[int | None],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))
    content = b"\x89PNG" + b"\0" * _OVER_DEFAULT_READ

    response = await client.upload_attachment(
        pb.UploadAttachmentRequest(pr_url=_PR, name="shot.png", content=content)
    )

    assert payload_dict(response.payload) == {"object": "test"}
    assert [call.method for call in host_calls] == ["UploadAttachment"]
    assert [(call[0], call[2]["content"] == content) for call in owner_calls] == [
        ("upload_pr_attachment", True)
    ]
    assert read_limits == [MAX_ATTACHMENT_BYTES + 64 * 1024]


async def test_a_non_owners_upload_is_refused_before_any_body_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    read_limits: list[int | None],
) -> None:
    from omnigent.server.routes.sessions import routes_resources

    monkeypatch.setattr(routes_resources, "_is_session_owner", lambda *_args, **_kwargs: False)
    app = service_tests._server_app(tmp_path, monkeypatch)
    monkeypatch.setattr(server_prs, "open_store", lambda _conversations: _Store(_linked(_PR)))
    request = pb.UploadAttachmentRequest(pr_url=_PR, name="shot.png", content=b"png")
    headers = {"Content-Type": "application/proto"}
    body_read: list[bytes] = []

    async def unread_body(scope: Any, receive: Any, send: Any) -> None:
        async def receive_noting_the_body() -> Any:
            message = await receive()
            if message.get("type") == "http.request" and message.get("body"):
                body_read.append(message["body"])
            return message

        await app(scope, receive_noting_the_body, send)

    response = await _post(unread_body, "UploadAttachment", request.SerializeToString(), headers)

    assert response.status_code == 403, response.text
    assert body_read == []
    assert read_limits == []
    assert host_calls == []


async def test_any_other_call_keeps_the_default_read_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    read_limits: list[int | None],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    with pytest.raises(ConnectError) as refused:
        await client.create_comment(
            pb.CreateCommentRequest(pr_url=_PR, body="x" * _OVER_DEFAULT_READ)
        )

    assert refused.value.code == Code.RESOURCE_EXHAUSTED
    assert read_limits == [DEFAULT_READ_MAX_BYTES]
    assert host_calls == []


async def test_an_upload_larger_than_an_attachment_is_refused_before_the_vouch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    store = _Store(_linked(_PR))
    client = _client(tmp_path, monkeypatch, store)
    content = b"\0" * (MAX_ATTACHMENT_BYTES + 1)

    with pytest.raises(ConnectError) as refused:
        await client.upload_attachment(
            pb.UploadAttachmentRequest(pr_url=_PR, name="clip.mp4", content=content)
        )

    assert (refused.value.code, refused.value.message) == (
        Code.INVALID_ARGUMENT,
        "The attachment is larger than 70 MB",
    )
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


def _tunnel(
    tmp_path: Path,
    *,
    refuse_send: bool = False,
    viewer: PullRequestViewer = PullRequestViewer.VIEWER,
    vouch: Callable[[str], Any] = _no_vouch,
    read_only_code_hosts: tuple[str, ...] = (),
) -> _Tunnel:
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
            _SESSION,
            vouch=vouch,
            overview=_as_reported,
            viewer=viewer,
            read_only_code_hosts=read_only_code_hosts,
        ),
    )
    return _Tunnel(registry, host, service, sent)


_MERGE = host_tests._OWNER_CALLS["MergePullRequest"][0]
_COMMENT_UNCONFIRMED = "Could not confirm comment posting. Check GitHub before retrying."
_CHANGE_UNCONFIRMED = "Could not confirm the pull request change. Check GitHub before retrying."
_UPLOAD_UNCONFIRMED = "Could not confirm the upload. Try attaching the file again."
_COMMENT_METHODS = {"CreateComment", "CreateReviewThread", "ReplyToReviewThread"}


def _unconfirmed_for(method: str) -> str:
    if method == "UploadAttachment":
        return _UPLOAD_UNCONFIRMED
    return _COMMENT_UNCONFIRMED if method in _COMMENT_METHODS else _CHANGE_UNCONFIRMED


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


# The lifecycle changes of one pull request, which answer as any pull request change does.
_LIFECYCLE_WRITES = frozenset({"SetPullRequestState", "SetPullRequestDraft"})
_REVIEW_WRITES = sorted(
    set(host_tests._GITHUB_CHANGE_CALLS) - {"GetPendingReview"} - _LIFECYCLE_WRITES
)
# The pull request changes; the review changes and the auto-merge batch have their own cases.
_WRITES = sorted(_PULL_REQUEST_WRITES - set(_REVIEW_WRITES) - {"EnableAutoMerge"})
# A valid request for each of those changes.
_WRITE_REQUESTS = {
    **host_tests._OWNER_CALLS,
    **host_tests._QUEUE_CALLS,
    **{method: host_tests._GITHUB_CHANGE_CALLS[method] for method in _LIFECYCLE_WRITES},
}


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
    assert set(_WRITES) <= set(_WRITE_REQUESTS)
    assert len(_WRITES) == 15


@pytest.mark.parametrize("method", _WRITES)
async def test_a_host_failure_after_a_change_was_sent_is_unconfirmed(
    tmp_path: Path, method: str
) -> None:
    """A host error shaping GitHub's answer may follow a change GitHub made."""
    tunnel = _tunnel(tmp_path)
    error = await _end_after_frame(tunnel, method, _WRITE_REQUESTS[method][0], "internal")

    assert (error.code, error.message) == (Code.UNKNOWN, _unconfirmed_for(method))


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


async def test_an_upload_waits_as_long_as_the_rest_route_for_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large upload outlasts a change's 20 s; only the upload gets the longer wait."""
    monkeypatch.setattr(workspace_api, "_WRITE_REPLY_TIMEOUT_S", 60.0)
    monkeypatch.setattr(workspace_api, "_UPLOAD_REPLY_TIMEOUT_S", 0.05)
    tunnel = _tunnel(tmp_path)
    upload = host_tests._OWNER_CALLS["UploadAttachment"][0]

    with pytest.raises(ConnectError) as lost:
        await tunnel.call("UploadAttachment", upload)

    assert (lost.value.code, lost.value.message) == (Code.DEADLINE_EXCEEDED, _UPLOAD_UNCONFIRMED)
    assert workspace_api._reply_timeout("UploadAttachment", changes=True) == 0.05
    assert workspace_api._reply_timeout("CreateComment", changes=True) == 60.0


async def test_a_frame_over_the_tunnels_cap_is_never_sent_and_the_host_stays_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message over the cap would close the host's connection, for every session on it."""
    monkeypatch.setattr(workspace_api, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", 4096)
    tunnel = _tunnel(tmp_path)

    with pytest.raises(ConnectError) as refused:
        await tunnel.call("CreateComment", pb.CreateCommentRequest(pr_url=_PR, body="x" * 4096))

    assert refused.value.code == Code.RESOURCE_EXHAUSTED
    assert tunnel.sent == []
    assert tunnel.registry.get("host_ws") is tunnel.host
    assert not tunnel.host.pending_workspace_calls


async def test_the_largest_upload_allowed_fits_in_one_frame(tmp_path: Path) -> None:
    """The longest name and URL, the name in characters JSON escapes, with a full file."""
    longest = pb.UploadAttachmentRequest(
        pr_url="https://github.com/" + "o" * (MAX_PULL_REQUEST_URL_CHARACTERS - 19),
        name="\U0001f600" * MAX_ATTACHMENT_NAME_CHARACTERS,
        content=b"\xff" * MAX_ATTACHMENT_BYTES,
    )
    tunnel = _tunnel(tmp_path)

    error = await _end_after_frame(tunnel, "UploadAttachment", longest, "internal")

    assert (error.code, error.message) == (Code.UNKNOWN, _UPLOAD_UNCONFIRMED)
    assert len(encode_host_frame(tunnel.sent[0])) <= RUNNER_TUNNEL_MAX_MESSAGE_BYTES


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
    image_reads: list[tuple[str, str, str]],
) -> None:
    """A call made anyway gets the host's UNIMPLEMENTED, which says nothing was changed."""
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    listed = await client.get_capabilities(pb.GetCapabilitiesRequest())
    with pytest.raises(ConnectError) as refused:
        await client.create_comment(_CREATE_COMMENT)
    image = pb.ReadPullRequestImageRequest(pr_url=_PR, image_url=host_tests._IMAGE_URL)
    with pytest.raises(ConnectError) as image_refused:
        await _image(client, image)

    assert set(listed.capabilities) >= _PULL_REQUEST_READS
    assert not set(listed.capabilities) & _PULL_REQUEST_OWNER_METHODS
    assert "ReadPullRequestImage" in _PULL_REQUEST_OWNER_METHODS
    assert refused.value.code == Code.UNIMPLEMENTED
    assert image_refused.value.code == Code.UNIMPLEMENTED
    assert [call.method for call in host_calls] == [
        "GetCapabilities",
        "CreateComment",
        "ReadPullRequestImage",
    ]
    assert owner_calls == []
    assert image_reads == []


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


def _failure_reasons(error: ConnectError) -> list[str]:
    """The PullRequestFailure reasons an error carries, as a client reads them."""
    return [
        pb.PullRequestFailure.FromString(detail.message_bytes).reason
        for detail in error.details
        if detail.type_name == pb.PullRequestFailure.DESCRIPTOR.full_name
    ]


@pytest.fixture
def github_changes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Each GitHub review, lifecycle, merge queue or stack call a method made on the host."""
    return host_tests.record_github_changes(monkeypatch)


@pytest.mark.parametrize("method", sorted(host_tests._SELECTED_PR_CALLS))
async def test_a_github_call_to_a_linked_pr_runs_on_the_servers_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    github_changes: list[tuple[str, str, str, dict[str, Any]]],
    method: str,
) -> None:
    request, function, _kwargs = host_tests._SELECTED_PR_CALLS[method]
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    response = await getattr(client, WORKSPACE_METHODS[method].python_name)(request)

    assert payload_dict(response.payload) == {"object": "test"}
    assert [(call.session_id, call.vouch) for call in host_calls] == [
        (_SESSION, PullRequestVouch(server_tracked=True)),
    ]
    assert [(call[0], call[2]) for call in github_changes] == [(function, _PR)]


@pytest.mark.parametrize("method", sorted(host_tests._SELECTED_PR_CALLS))
async def test_a_github_call_to_an_unlinked_pr_is_refused_before_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    github_changes: list[tuple[str, str, str, dict[str, Any]]],
    method: str,
) -> None:
    request, _function, _kwargs = host_tests._SELECTED_PR_CALLS[method]
    client = _client(tmp_path, monkeypatch, _Store(_linked(_OTHER_PR)))

    with pytest.raises(ConnectError) as refused:
        await getattr(client, WORKSPACE_METHODS[method].python_name)(request)

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert host_calls == []
    assert github_changes == []


@pytest.fixture
def auto_merges(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str, str]]]:
    """Each auto-merge batch the host sent: its pull requests, methods and heads."""
    return host_tests.record_auto_merges(monkeypatch)


async def test_auto_merge_of_linked_prs_runs_on_the_servers_word_for_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    auto_merges: list[list[tuple[str, str, str]]],
) -> None:
    store = _Store(_linked(_PR, _OTHER_PR))
    client = _client(tmp_path, monkeypatch, store)

    response = await client.enable_auto_merge(host_tests._AUTO_MERGE)

    assert payload_dict(response.payload) == {"object": "test"}
    assert [(call.session_id, call.vouch) for call in host_calls] == [
        (_SESSION, PullRequestVouch(server_tracked=True)),
    ]
    assert store.reads == 2
    assert [[url for url, _method, _head in batch] for batch in auto_merges] == [[_PR, _OTHER_PR]]


async def test_auto_merge_naming_an_unlinked_pr_is_refused_before_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    auto_merges: list[list[tuple[str, str, str]]],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    with pytest.raises(ConnectError) as refused:
        await client.enable_auto_merge(host_tests._AUTO_MERGE)

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert host_calls == []
    assert auto_merges == []


async def test_auto_merge_before_the_server_stores_the_prs_leaves_each_to_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    auto_merges: list[list[tuple[str, str, str]]],
) -> None:
    """Without the server's word for every PR, the host checks each against its record."""
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR, _OTHER_PR, synced=False)))

    with pytest.raises(ConnectError) as refused:
        await client.enable_auto_merge(host_tests._AUTO_MERGE)

    assert [call.vouch for call in host_calls] == [None]
    assert (refused.value.code, refused.value.message) == (
        Code.FAILED_PRECONDITION,
        "This pull request is not associated with the session",
    )
    assert auto_merges == []


@pytest.mark.parametrize(
    "items, refusal",
    [
        ([], "Enable auto-merge for 1 to 20 pull requests at once"),
        (
            [pb.PullRequestAutoMerge(merge_method="squash", expected_head_oid=host_tests._HEAD)],
            "Name each pull request to merge automatically",
        ),
    ],
    ids=["none", "unnamed"],
)
async def test_an_auto_merge_batch_the_server_cant_vouch_for_is_refused_before_the_vouch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    items: list[pb.PullRequestAutoMerge],
    refusal: str,
) -> None:
    store = _Store(_linked(_PR))
    client = _client(tmp_path, monkeypatch, store)

    with pytest.raises(ConnectError) as refused:
        await client.enable_auto_merge(pb.EnableAutoMergeRequest(pull_requests=items))

    assert (refused.value.code, refused.value.message) == (Code.INVALID_ARGUMENT, refusal)
    assert store.reads == 0
    assert host_calls == []


_AUTO_MERGE_UNCONFIRMED = (
    "Could not confirm which pull requests will merge automatically. Check GitHub before retrying."
)


async def test_an_auto_merge_batch_waits_longer_and_its_lost_answer_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch reads and changes up to 20 pull requests, past a single change's 20 s."""
    assert workspace_api._reply_timeout("EnableAutoMerge", changes=True) == 120.0
    assert workspace_api._reply_timeout("SetPullRequestState", changes=True) == 20.0
    monkeypatch.setattr(workspace_api, "_WRITE_REPLY_TIMEOUT_S", 60.0)
    monkeypatch.setattr(workspace_api, "_AUTO_MERGE_REPLY_TIMEOUT_S", 0.05)
    tunnel = _tunnel(tmp_path)

    with pytest.raises(ConnectError) as lost:
        await tunnel.call("EnableAutoMerge", host_tests._AUTO_MERGE)

    assert (lost.value.code, lost.value.message) == (
        Code.DEADLINE_EXCEEDED,
        _AUTO_MERGE_UNCONFIRMED,
    )
    assert len(tunnel.sent) == 1


async def test_a_malformed_review_change_is_refused_before_the_vouch_as_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    store = _Store(_linked(_PR))
    client = _client(tmp_path, monkeypatch, store)

    with pytest.raises(ConnectError) as refused:
        await client.add_reaction(
            pb.AddReactionRequest(pr_url=_PR, kind="issue", comment_id=5, content="MEH")
        )

    assert refused.value.code == Code.INVALID_ARGUMENT
    assert _failure_reasons(refused.value) == ["invalid_input"]
    assert store.reads == 0
    assert host_calls == []


async def test_a_hosts_refusal_reason_reaches_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    from omnigent.runner import github_reviews
    from omnigent.runner.pull_request_errors import CommentChangedError

    def changed(_root: str, _reference: Any, **_kwargs: Any) -> dict[str, Any]:
        raise CommentChangedError()

    monkeypatch.setattr(github_reviews, "edit_comment", changed)
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    with pytest.raises(ConnectError) as refused:
        await client.edit_comment(host_tests._GITHUB_CHANGE_CALLS["EditComment"][0])

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert _failure_reasons(refused.value) == ["comment_changed"]


@pytest.mark.parametrize(
    ("reason", "attached"),
    [
        ("comment_changed", ["comment_changed"]),
        ("pull_request_changed", ["pull_request_changed"]),
        ("invalid_input", ["invalid_input"]),
        ("read_only_code_host", ["read_only_code_host"]),
        ("merge_queue_full", []),
        ("<b>Click here</b>", []),
        ("", []),
        (None, []),
    ],
    ids=["comment", "head", "input", "read-only", "other-known", "arbitrary", "empty", "none"],
)
async def test_only_an_allowed_reason_from_the_host_is_attached(
    tmp_path: Path, reason: str | None, attached: list[str]
) -> None:
    """A host can't push its own text into the structured field a client acts on."""
    tunnel = _tunnel(tmp_path)
    request = host_tests._GITHUB_CHANGE_CALLS["EditComment"][0]
    call = asyncio.create_task(tunnel.call("EditComment", request))
    await tunnel.frame_sent()
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=tunnel.sent[0].request_id,
            done=True,
            error_code="failed_precondition",
            error_message="Refused",
            error_reason=reason,
        ),
    )

    with pytest.raises(ConnectError) as refused:
        await call

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert _failure_reasons(refused.value) == attached
    assert len(refused.value.details) == len(attached)


def test_a_pull_request_failure_carries_only_its_reason() -> None:
    assert [field.name for field in pb.PullRequestFailure.DESCRIPTOR.fields] == ["reason"]
    assert {
        "comment_changed",
        "pull_request_changed",
        "invalid_input",
        "read_only_code_host",
        *github_images.IMAGE_REFUSAL_REASONS,
    } == workspace_api.PULL_REQUEST_FAILURE_REASONS


_REVIEW_UNCONFIRMED = "Could not confirm the review change. Check GitHub before retrying."


@pytest.mark.parametrize("method", _REVIEW_WRITES)
async def test_a_review_change_whose_host_drops_after_the_frame_is_unconfirmed(
    tmp_path: Path, method: str
) -> None:
    tunnel = _tunnel(tmp_path)
    request = host_tests._GITHUB_CHANGE_CALLS[method][0]
    call = asyncio.create_task(tunnel.call(method, request))
    await tunnel.frame_sent()
    tunnel.registry.deregister("host_ws", conn=tunnel.host)

    with pytest.raises(ConnectError) as lost:
        await call

    assert (lost.value.code, lost.value.message) == (Code.UNKNOWN, _REVIEW_UNCONFIRMED)


@pytest.mark.parametrize("method", _REVIEW_WRITES)
async def test_a_host_failure_after_a_review_change_was_sent_is_unconfirmed(
    tmp_path: Path, method: str
) -> None:
    """A host error shaping GitHub's answer may follow a change GitHub made."""
    tunnel = _tunnel(tmp_path)
    error = await _end_after_frame(
        tunnel, method, host_tests._GITHUB_CHANGE_CALLS[method][0], "internal"
    )

    assert (error.code, error.message) == (Code.UNKNOWN, _REVIEW_UNCONFIRMED)


async def test_a_host_failure_on_the_pending_review_read_stays_internal(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path)
    error = await _end_after_frame(
        tunnel,
        "GetPendingReview",
        host_tests._GITHUB_CHANGE_CALLS["GetPendingReview"][0],
        "internal",
    )

    assert (error.code, error.message) == (Code.INTERNAL, "Workspace call failed")


async def test_a_review_change_whose_answer_times_out_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_api, "_WRITE_REPLY_TIMEOUT_S", 0.05)
    tunnel = _tunnel(tmp_path)

    with pytest.raises(ConnectError) as lost:
        await tunnel.call(
            "SubmitPendingReview", host_tests._GITHUB_CHANGE_CALLS["SubmitPendingReview"][0]
        )

    assert (lost.value.code, lost.value.message) == (Code.DEADLINE_EXCEEDED, _REVIEW_UNCONFIRMED)


async def test_the_pending_review_read_cut_off_by_the_host_stays_safe_to_retry(
    tmp_path: Path,
) -> None:
    tunnel = _tunnel(tmp_path)
    call = asyncio.create_task(
        tunnel.call("GetPendingReview", host_tests._GITHUB_CHANGE_CALLS["GetPendingReview"][0])
    )
    await tunnel.frame_sent()
    tunnel.registry.deregister("host_ws", conn=tunnel.host)

    with pytest.raises(ConnectError) as cut_off:
        await call

    assert cut_off.value.code == Code.UNAVAILABLE


_HOST_THREADS = {
    "data": [
        {
            "id": "PRRT_1",
            "can_reply": True,
            "comments": {"data": [{"id": "PRRC_1", "database_id": 2**53 - 1, "can_update": True}]},
        }
    ]
}


async def _threads_through(tmp_path: Path, viewer: PullRequestViewer) -> dict[str, Any]:
    tunnel = _tunnel(tmp_path, viewer=viewer)
    call = asyncio.create_task(
        tunnel.call("ListReviewThreads", pb.ListReviewThreadsRequest(pr_url=_PR))
    )
    await tunnel.frame_sent()
    answer = pb.ListReviewThreadsResponse(payload=payload_struct(_HOST_THREADS))
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=tunnel.sent[0].request_id,
            message=answer.SerializeToString(),
            done=True,
        ),
    )
    return payload_dict((await call).payload)


async def test_the_owner_reads_threads_with_the_host_accounts_rights(tmp_path: Path) -> None:
    """The largest comment number the host sends survives the payload's doubles exactly."""
    threads = await _threads_through(tmp_path, PullRequestViewer.OWNER)
    assert threads == _HOST_THREADS
    [thread] = threads["data"]
    number = thread["comments"]["data"][0]["database_id"]
    assert int(number) == number == 2**53 - 1
    assert pb.EditCommentRequest(comment_id=int(number)).comment_id == 2**53 - 1


async def test_anyone_else_reads_threads_through_the_api_without_the_rights(
    tmp_path: Path,
) -> None:
    [thread] = (await _threads_through(tmp_path, PullRequestViewer.VIEWER))["data"]
    [comment] = thread["comments"]["data"]
    assert (thread["id"], thread["can_reply"]) == ("PRRT_1", False)
    assert (comment["database_id"], comment["can_update"]) == (2**53 - 1, False)


@pytest.mark.parametrize("method", ["ListReviewThreads", "ListReviewThreadComments"])
@pytest.mark.parametrize(
    ("viewer", "asks"),
    [
        (PullRequestViewer.OWNER, True),
        (PullRequestViewer.EDITOR, False),
        (PullRequestViewer.VIEWER, False),
    ],
)
async def test_the_frame_asks_for_the_accounts_rights_for_the_owner_alone(
    tmp_path: Path, method: str, viewer: PullRequestViewer, asks: bool
) -> None:
    """The ask is the server's, in the frame; a request message can't carry it."""
    tunnel = _tunnel(tmp_path, viewer=viewer)
    request = (
        pb.ListReviewThreadsRequest(pr_url=_PR)
        if method == "ListReviewThreads"
        else pb.ListReviewThreadCommentsRequest(pr_url=_PR, thread_id="PRRT_1", page_token="c")
    )
    call = asyncio.create_task(tunnel.call(method, request))
    await tunnel.frame_sent()
    call.cancel()

    [frame] = tunnel.sent
    assert frame.account_rights is asks


async def test_no_other_pull_request_call_asks_for_the_accounts_rights(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path, viewer=PullRequestViewer.OWNER)
    call = asyncio.create_task(tunnel.call("GetPullRequest", pb.GetPullRequestRequest(pr_url=_PR)))
    await tunnel.frame_sent()
    call.cancel()

    [frame] = tunnel.sent
    assert frame.account_rights is False


def test_the_route_takes_the_reader_to_be_the_least_trusted_unless_told() -> None:
    route = workspace_api.PullRequestRoute(_SESSION, vouch=_no_vouch, overview=_as_reported)
    assert route.viewer is PullRequestViewer.VIEWER
    assert {"ListReviewThreads", "ListReviewThreadComments"} == workspace_api.VIEWER_SHAPED_METHODS


@pytest.mark.parametrize("method", ["ListReviewThreads", "ListReviewThreadComments"])
async def test_a_failed_owner_lookup_reads_threads_without_the_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """As on REST: unsure who's reading, the read still answers, with no one's rights."""
    from omnigent.server.routes.sessions import routes_resources

    def lookup_fails(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("permission store unavailable")

    monkeypatch.setattr(routes_resources, "_is_session_owner", lookup_fails)
    monkeypatch.setattr(
        WorkspaceReader, "github_review_threads", lambda _self, *_args, **_kwargs: _HOST_THREADS
    )
    comments = _HOST_THREADS["data"][0]["comments"]
    monkeypatch.setattr(
        WorkspaceReader, "github_review_thread_comments", lambda _self, *_args, **_kwargs: comments
    )
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    if method == "ListReviewThreads":
        response = await client.list_review_threads(pb.ListReviewThreadsRequest(pr_url=_PR))
        [thread] = payload_dict(response.payload)["data"]
        assert thread["can_reply"] is False
        [comment] = thread["comments"]["data"]
    else:
        response = await client.list_review_thread_comments(
            pb.ListReviewThreadCommentsRequest(pr_url=_PR, thread_id="PRRT_1")
        )
        [comment] = payload_dict(response.payload)["data"]
    assert comment["can_update"] is False


@pytest.mark.parametrize("owner", [True, False], ids=["owner", "non-owner"])
async def test_the_routes_owner_lookup_decides_who_sees_the_thread_rights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: bool,
) -> None:
    from omnigent.server.routes.sessions import routes_resources

    monkeypatch.setattr(routes_resources, "_is_session_owner", lambda *_a, **_k: owner)
    monkeypatch.setattr(
        WorkspaceReader, "github_review_threads", lambda _self, *_args, **_kwargs: _HOST_THREADS
    )
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    response = await client.list_review_threads(pb.ListReviewThreadsRequest(pr_url=_PR))

    [thread] = payload_dict(response.payload)["data"]
    assert thread["can_reply"] is owner
    assert thread["comments"]["data"][0]["can_update"] is owner


# Every change to the pull request on its code host, with a valid request for it.
_CODE_HOST_CHANGES = sorted(_PULL_REQUEST_WRITES - {"SetPullRequestAccount"})
_CHANGE_REQUESTS = {
    **host_tests._OWNER_CALLS,
    **host_tests._GITHUB_CHANGE_CALLS,
    **host_tests._QUEUE_CALLS,
    "EnableAutoMerge": (host_tests._AUTO_MERGE, "enable_auto_merge", {}),
}
_READ_ONLY_GITHUB = ("github.com",)


@pytest.mark.parametrize(
    ("read_only_code_hosts", "capabilities", "marked"),
    [
        pytest.param(
            _READ_ONLY_GITHUB,
            {"merging": True, "line_counts": True},
            {"read_only": True, "capabilities": {"merging": False, "line_counts": True}},
            id="read-only",
        ),
        pytest.param(_READ_ONLY_GITHUB, None, {"read_only": True}, id="older-host"),
        pytest.param(("git.example.com",), {"merging": True}, {}, id="writable"),
    ],
)
# The overview is shaped as well when the server has no PR store or can't store the PRs.
@pytest.mark.parametrize(
    "fault", [None, "no-store", "ingest-fails"], ids=["stored", "no-store", "ingest-fails"]
)
async def test_the_overview_of_a_pr_on_a_read_only_host_offers_no_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only_code_hosts: tuple[str, ...],
    capabilities: dict[str, bool] | None,
    marked: dict[str, Any],
    fault: str | None,
) -> None:
    """The workspace API shapes the host's overview as the REST route does."""
    overview: dict[str, Any] = {"selected_pr_url": _PR, "pr": {"url": _PR}}
    if capabilities is not None:
        overview["capabilities"] = capabilities
    stored = _linked(_PR)

    def ingest(*_args: Any, **_kwargs: Any) -> Any:
        if fault == "ingest-fails":
            raise OSError("disk full")
        return stored

    monkeypatch.setattr(server_prs, "ingest_info", ingest)
    monkeypatch.setattr(server_prs, "answer", lambda info, _stored: dict(info))
    monkeypatch.setattr(
        WorkspaceReader, "github_info", lambda _self, _session, _url: dict(overview)
    )
    store = None if fault == "no-store" else _Store(stored)
    client = _client(tmp_path, monkeypatch, store, read_only_code_hosts=read_only_code_hosts)

    response = await client.get_pull_request(pb.GetPullRequestRequest(pr_url=_PR))

    assert payload_dict(response.payload) == {**overview, **marked}


async def _tracked(_pr_url: str) -> PullRequestVouch:
    """The vouch once the server stores the session's PRs: their first is the default."""
    return PullRequestVouch(server_tracked=True, default_pr_url=_PR)


async def _refused_before_the_host(tunnel: _Tunnel, method: str, request: Any) -> ConnectError:
    """The read-only refusal a change gets, checking that no frame went to the host."""
    with pytest.raises(ConnectError) as refused:
        # A change that isn't refused waits for the host; this one has no answer.
        await asyncio.wait_for(tunnel.call(method, request), timeout=5)
    assert refused.value.code == Code.FAILED_PRECONDITION
    assert refused.value.message == READ_ONLY_CODE_HOST_MESSAGE
    assert _failure_reasons(refused.value) == ["read_only_code_host"]
    assert tunnel.sent == []
    return refused.value


@pytest.mark.parametrize("method", _CODE_HOST_CHANGES)
async def test_a_change_to_a_pr_on_a_read_only_host_is_refused_before_the_host(
    tmp_path: Path, method: str
) -> None:
    tunnel = _tunnel(tmp_path, vouch=_tracked, read_only_code_hosts=_READ_ONLY_GITHUB)

    await _refused_before_the_host(tunnel, method, _CHANGE_REQUESTS[method][0])


async def test_an_auto_merge_batch_naming_a_read_only_hosts_pr_is_refused_before_the_host(
    tmp_path: Path,
) -> None:
    """The session's first pull request is writable; a later one in the batch isn't."""
    batch = pb.EnableAutoMergeRequest(
        pull_requests=[
            pb.PullRequestAutoMerge(pr_url=_PR, merge_method="squash", expected_head_oid="a" * 40),
            pb.PullRequestAutoMerge(
                pr_url="https://git.example.com/example-org/example-repo/pull/9",
                merge_method="squash",
                expected_head_oid="b" * 40,
            ),
        ]
    )
    tunnel = _tunnel(tmp_path, vouch=_tracked, read_only_code_hosts=("git.example.com",))

    await _refused_before_the_host(tunnel, "EnableAutoMerge", batch)


async def test_an_auto_merge_batch_on_writable_hosts_reaches_the_host(tmp_path: Path) -> None:
    """A batch names no single pull request, so each one it names is checked instead."""
    tunnel = _tunnel(tmp_path, vouch=_tracked, read_only_code_hosts=("git.example.com",))

    await _end_after_frame(tunnel, "EnableAutoMerge", host_tests._AUTO_MERGE, "internal")

    assert [frame.method for frame in tunnel.sent] == ["EnableAutoMerge"]


@pytest.mark.parametrize(
    "request_message",
    [
        pb.CreateCommentRequest(body="Looks good"),
        pb.CreateCommentRequest(pr_url="\u00a0" + _PR, body="Looks good"),
        pb.CreateCommentRequest(
            pr_url="https://example.visualstudio.com/project/_git/repo/pullrequest/7",
            body="Looks good",
        ),
    ],
    ids=["no-pr", "padded-url", "legacy-alias"],
)
async def test_a_change_the_server_cant_place_off_a_read_only_host_is_refused(
    tmp_path: Path, request_message: Any
) -> None:
    """Before the server stores the session's PRs, the host picks or normalizes the PR."""
    hosts = (*_READ_ONLY_GITHUB, "dev.azure.com")
    tunnel = _tunnel(tmp_path, vouch=_no_vouch, read_only_code_hosts=hosts)

    await _refused_before_the_host(tunnel, "CreateComment", request_message)


async def test_a_change_naming_no_pr_is_refused_when_the_first_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    """Through the route, so the server's configured hosts reach the workspace API."""
    client = _client(
        tmp_path, monkeypatch, _Store(_linked(_PR)), read_only_code_hosts=_READ_ONLY_GITHUB
    )

    with pytest.raises(ConnectError) as refused:
        await client.create_comment(pb.CreateCommentRequest(body="Looks good"))

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert refused.value.message == READ_ONLY_CODE_HOST_MESSAGE
    assert _failure_reasons(refused.value) == ["read_only_code_host"]
    assert host_calls == []


async def test_a_change_naming_no_pr_reaches_the_host_when_the_first_is_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """Another host being read-only doesn't hold back a change to the session's first PR."""
    client = _client(
        tmp_path, monkeypatch, _Store(_linked(_PR)), read_only_code_hosts=("git.example.com",)
    )

    await client.create_comment(pb.CreateCommentRequest(body="Looks good"))

    assert [call.method for call in host_calls] == ["CreateComment"]
    [(function, _root, arguments)] = owner_calls
    assert function == "create_pr_comment"
    # Forwarded as sent; the real host then refuses a comment that names no PR.
    assert arguments["pr_url"] is None
    assert arguments["body"] == "Looks good"


@pytest.mark.parametrize("method", ["SetPullRequestAccount", "GetMergeOptions"])
async def test_reads_and_the_account_choice_on_a_read_only_host_reach_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    owner_calls: list[tuple[str, str, dict[str, Any]]],
    method: str,
) -> None:
    client = _client(
        tmp_path, monkeypatch, _Store(_linked(_PR)), read_only_code_hosts=_READ_ONLY_GITHUB
    )

    await getattr(client, WORKSPACE_METHODS[method].python_name)(_CHANGE_REQUESTS[method][0])

    assert [call.method for call in host_calls] == [method]
    assert len(owner_calls) == 1


# -- An image read streams through the server for the session's owner. -------------------


@pytest.fixture
def image_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    return host_tests.record_image_reads(monkeypatch)


async def _image(client: Any, request: pb.ReadPullRequestImageRequest) -> bytes:
    """Every part of an image read, joined, after checking its metadata comes first."""
    replies = [reply async for reply in client.read_pull_request_image(request)]
    assert replies[0].HasField("metadata")
    assert not any(reply.HasField("metadata") for reply in replies[1:])
    assert replies[0].metadata.size == sum(len(reply.data) for reply in replies)
    return b"".join(reply.data for reply in replies)


async def test_an_image_of_a_linked_pr_streams_through_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    image_reads: list[tuple[str, str, str]],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))

    image = await _image(
        client, pb.ReadPullRequestImageRequest(pr_url=_PR, image_url=host_tests._IMAGE_URL)
    )

    assert image == host_tests._IMAGE.data
    assert image_reads == [(str(tmp_path.resolve()), _PR, host_tests._IMAGE_URL)]
    assert [(call.method, call.session_id) for call in host_calls] == [
        ("ReadPullRequestImage", _SESSION)
    ]


async def test_an_image_of_an_unlinked_pr_is_refused_before_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    image_reads: list[tuple[str, str, str]],
) -> None:
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))
    request = pb.ReadPullRequestImageRequest(pr_url=_OTHER_PR, image_url=host_tests._IMAGE_URL)

    with pytest.raises(ConnectError) as refused:
        await _image(client, request)

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert host_calls == []
    assert image_reads == []


async def test_an_image_read_with_no_url_is_refused_before_the_vouch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    store = _Store(_linked(_PR))
    client = _client(tmp_path, monkeypatch, store)

    with pytest.raises(ConnectError) as refused:
        await _image(client, pb.ReadPullRequestImageRequest(pr_url=_PR))

    assert (refused.value.code, refused.value.message) == (
        Code.INVALID_ARGUMENT,
        "Expected an image URL",
    )
    assert _failure_reasons(refused.value) == ["invalid_input"]
    assert store.reads == 0
    assert host_calls == []


async def test_an_image_refusal_reaches_the_client_with_its_reason_and_no_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_root: str, _reference: Any, _image_url: str) -> Any:
        raise github_images.ImageRefused("host_not_allowed")

    monkeypatch.setattr(github_images, "read_pull_request_image", refuse)
    client = _client(tmp_path, monkeypatch, _Store(_linked(_PR)))
    signed = "https://private-user-images.githubusercontent.com/1/a.png?jwt=secret"

    with pytest.raises(ConnectError) as refused:
        await _image(client, pb.ReadPullRequestImageRequest(pr_url=_PR, image_url=signed))

    assert refused.value.code == Code.FAILED_PRECONDITION
    assert refused.value.message == "GitHub won't show images from that address."
    assert _failure_reasons(refused.value) == ["host_not_allowed"]


def _image_stream(tunnel: _Tunnel) -> Any:
    request = pb.ReadPullRequestImageRequest(pr_url=_PR, image_url=host_tests._IMAGE_URL)
    return tunnel.service.read_pull_request_image(request, None)  # type: ignore[call-arg]


async def test_a_host_failure_after_the_first_part_ends_the_image_read(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path)
    stream = _image_stream(tunnel)
    first = asyncio.create_task(anext(stream))
    await tunnel.frame_sent()
    request_id = tunnel.sent[0].request_id
    metadata = pb.ReadPullRequestImageResponse(
        metadata=pb.PullRequestImageMetadata(content_type="image/png", size=10)
    )
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(request_id=request_id, message=metadata.SerializeToString()),
    )
    assert (await first).metadata.size == 10
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=request_id,
            done=True,
            error_code="unavailable",
            error_message="GitHub couldn't load that image. Try again.",
            error_reason="fetch_failed",
        ),
    )

    with pytest.raises(ConnectError) as failed:
        await anext(stream)

    assert failed.value.code == Code.UNAVAILABLE
    assert _failure_reasons(failed.value) == ["fetch_failed"]
    assert tunnel.host.pending_workspace_calls == {}


async def test_an_image_read_waits_its_own_time_for_each_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first reply follows the PR read and the whole fetch, past a read's 30 s."""
    monkeypatch.setattr(workspace_api, "_IMAGE_REPLY_TIMEOUT_S", 0.05)
    tunnel = _tunnel(tmp_path)

    # Bounded here, so a read waiting only a read's 30 s fails the test at once.
    with pytest.raises(ConnectError) as lost:
        await asyncio.wait_for(anext(_image_stream(tunnel)), timeout=5.0)

    assert (lost.value.code, lost.value.message) == (
        Code.DEADLINE_EXCEEDED,
        "The session's host did not answer",
    )


async def test_an_image_part_after_the_metadata_waits_the_images_time_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Long enough to deliver the metadata under load; the part after it never comes.
    monkeypatch.setattr(workspace_api, "_IMAGE_REPLY_TIMEOUT_S", 0.5)
    tunnel = _tunnel(tmp_path)
    stream = _image_stream(tunnel)
    first = asyncio.create_task(anext(stream))
    await tunnel.frame_sent()
    metadata = pb.ReadPullRequestImageResponse(
        metadata=pb.PullRequestImageMetadata(content_type="image/png", size=10)
    )
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=tunnel.sent[0].request_id, message=metadata.SerializeToString()
        ),
    )
    await first

    with pytest.raises(ConnectError) as lost:
        await asyncio.wait_for(anext(stream), timeout=5.0)

    assert lost.value.code == Code.DEADLINE_EXCEEDED


def test_the_first_image_reply_wait_covers_every_phase_of_the_host_read() -> None:
    """Account lookup, token read, slot wait and fetch all come before the metadata."""
    from omnigent.runner import github_resource

    phases = (
        github_resource._PR_EDIT_REQUEST_SECONDS
        + github_images._READ_TOKEN_SECONDS
        + github_images.SLOT_WAIT_SECONDS
        + github_images.FETCH_DEADLINE_SECONDS
    )
    assert phases < workspace_api._IMAGE_REPLY_TIMEOUT_S


async def test_a_caller_that_leaves_an_image_read_ends_its_call(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path)
    stream = _image_stream(tunnel)
    first = asyncio.create_task(anext(stream))
    await tunnel.frame_sent()
    metadata = pb.ReadPullRequestImageResponse(
        metadata=pb.PullRequestImageMetadata(content_type="image/png", size=10)
    )
    deliver_workspace_reply(
        tunnel.host,
        HostWorkspaceReplyFrame(
            request_id=tunnel.sent[0].request_id, message=metadata.SerializeToString()
        ),
    )
    await first

    await stream.aclose()

    assert tunnel.host.pending_workspace_calls == {}
