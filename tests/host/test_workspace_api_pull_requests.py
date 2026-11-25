"""The server's route for the pull request reads, end to end through the host tunnel.

The server sends the authorized session and its vouch from the PRs it stores; the
host reads only what that allows. Each case counts the calls that reach the host and
the PR reads that get past the host's scope check. The callers' gates are in
``tests/server/routes/test_shell_permission_gate.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.protocol import ProtocolType

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
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
from omnigent.server import workspace_api
from omnigent.server.host_registry import HostConnection, HostRegistry, deliver_workspace_reply
from omnigent.server.workspace_access import METHOD_GATES, WorkspaceGate, callable_methods
from omnigent.stores.session_pull_request_store import SessionPullRequests
from omnigent.workspace_fs import WorkspaceReader
from tests.host import test_workspace_service as service_tests

_SESSION = service_tests._SESSION
_PR = "https://github.com/example/app/pull/7"
_OTHER_PR = "https://github.com/example/app/pull/8"
_PULL_REQUEST_METHODS = {name for name, m in WORKSPACE_METHODS.items() if m.pull_request}


@dataclass
class _Entry:
    url: str


class _Store:
    """The server's PR store, reduced to what the vouch reads."""

    def __init__(self, stored: SessionPullRequests | None = None, *, fails: bool = False) -> None:
        self.stored = stored
        self.fails = fails

    def read(self, _session_id: str) -> SessionPullRequests | None:
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
    assert {name: METHOD_GATES[name] for name in _PULL_REQUEST_METHODS} == {
        "GetPullRequest": WorkspaceGate.SESSION_OWNER,
        "ListPullRequestFiles": WorkspaceGate.BROWSE,
        "GetPullRequestDiff": WorkspaceGate.BROWSE,
        "ListPullRequestCommits": WorkspaceGate.BROWSE,
        "GetCommitDiff": WorkspaceGate.BROWSE,
        "ComparePullRequestRange": WorkspaceGate.BROWSE,
        "ListReviewThreads": WorkspaceGate.BROWSE,
        "ListReviewThreadComments": WorkspaceGate.READ,
    }


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


async def test_get_pull_request_stores_the_reported_prs_and_answers_with_the_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_calls: list[HostWorkspaceCallFrame],
    provider_reads: list[str | None],
) -> None:
    stored = _linked(_PR)
    ingested: list[dict[str, Any]] = []

    def ingest(_store: object, session_id: str, info: Any, *, requested_at: float) -> Any:
        assert session_id == _SESSION and requested_at > 0
        ingested.append(dict(info))
        return stored

    monkeypatch.setattr(server_prs, "ingest_info", ingest)
    monkeypatch.setattr(server_prs, "answer", lambda info, _stored: {**info, "prs": ["server"]})
    monkeypatch.setattr(
        WorkspaceReader,
        "github_info",
        lambda _self, _session, _url: {"pr": None, "prs": [{"url": _PR}], "count": 2},
    )
    client = _client(tmp_path, monkeypatch, _Store(stored))

    response = await client.get_pull_request(pb.GetPullRequestRequest(pr_url=_PR))
    assert ingested == [{"pr": None, "prs": [{"url": _PR}], "count": 2}]
    # The Struct carries numbers as floats; what's stored gets the integer back.
    assert type(ingested[0]["count"]) is int
    await client.list_pull_request_files(pb.ListPullRequestFilesRequest(pr_url=_PR))

    assert payload_dict(response.payload) == {"pr": None, "prs": ["server"], "count": 2}
    # The other reads store nothing.
    assert len(ingested) == 1
    assert [call.method for call in host_calls] == ["GetPullRequest", "ListPullRequestFiles"]


async def test_capabilities_through_the_server_list_the_pull_request_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    client = _client(tmp_path, monkeypatch, None)

    response = await client.get_capabilities(pb.GetCapabilitiesRequest())

    assert set(response.capabilities) >= _PULL_REQUEST_METHODS
    assert [(call.session_id, call.vouch) for call in host_calls] == [(_SESSION, None)]


async def _no_vouch(_pr_url: str) -> PullRequestVouch | None:
    return None


async def _as_reported(payload: dict[str, Any], _sent_at: float) -> dict[str, Any]:
    return payload


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
