"""Parity tests for the workspace API, on every route that serves it.

The API must answer exactly what the REST file routes answer, because both
read through :class:`WorkspaceReader`. Each parity test runs the real Connect
client on both routes (the host's service directly, and the server forwarding
over an in-memory host tunnel) and both browser-capable protocols, so neither
a route nor a protocol can change an answer. The reader's own input matrix
lives in ``tests/test_workspace_fs.py``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import signal
import socket
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.protocol import ProtocolType
from fastapi import FastAPI
from pyqwest import Client
from pyqwest.testing import ASGITransport

from omnigent.api.workspace.v1alpha1.workspace_connect import (
    WorkspaceServiceASGIApplication,
    WorkspaceServiceClient,
)
from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    CHANGE_STATUS_MODIFIED,
    CONTENT_ENCODING_UTF8,
    ENTRY_TYPE_DIRECTORY,
    FILE_SEARCH_MODE_SUBSTRING,
    GetCapabilitiesRequest,
    ListDirectoryRequest,
    ListRepositoryRefsRequest,
    ListWorkingCopyChangesRequest,
    ReadBaseFileRequest,
    ReadFileRequest,
    ReadLinkTextRequest,
    ResolveRepositoryContextRequest,
    SearchFilesRequest,
    SearchTextRequest,
    SearchTextResponse,
    StatPathRequest,
)
from omnigent.host.frames import (
    CAP_WORKSPACE_API,
    HostHelloFrame,
    HostWorkspaceCallFrame,
    HostWorkspaceReplyFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.workspace_service import (
    _SERVED,
    WorkspaceService,
    _Served,
    serve_tunnel_call,
    start_workspace_socket,
)
from omnigent.host.workspace_tokens import mint_token
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostConnection, HostRegistry, deliver_workspace_reply
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.polling import poll_until_async
from tests.server.routes.test_shell_permission_gate import _StubAgentStore, _StubConversationStore

pytestmark = pytest.mark.asyncio

_WITH_RG = {"SearchText"} if shutil.which("rg") else set()
PROTOCOLS = pytest.mark.parametrize("protocol", [ProtocolType.CONNECT, ProtocolType.GRPC_WEB])
_SESSION = "conv_ws"
_SERVER_BASE = f"http://server/v1/sessions/{_SESSION}/workspace"


def _workspace(root: Path) -> Path:
    """A workspace with text, binary, unicode, nested and multi-chunk files."""
    (root / "notes.md").write_text("# Title\nbody\n")
    (root / "café.txt").write_text("héllo")
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    (root / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
    (root / "big.txt").write_text("x" * 200_000)  # several 64 KiB chunks
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "empty.txt").write_bytes(b"")
    try:
        (root / "link.md").symlink_to("notes.md")
        (root / "broken").symlink_to("gone")
    except OSError:
        pass  # symlinks need extra privileges on some Windows machines
    return root


def _connect(app: Any, base: str, protocol: ProtocolType) -> WorkspaceServiceClient:
    return WorkspaceServiceClient(base, protocol=protocol, http_client=Client(ASGITransport(app)))


def _server_app(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: bool = True,
    capable: bool = True,
    sandbox: str | None = "none",
    flag: bool = True,
    side_chat: bool = False,
    remote_host: bool = False,
    content_search: bool = False,
) -> FastAPI:
    """The sessions API, with the session's host answering over an in-memory tunnel.

    Each keyword can break one precondition of the server route, make the
    session a side chat of a host-bound source, or put the host's tunnel on
    another replica.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from omnigent.errors import OmnigentError, WrongReplicaError
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import create_sessions_router
    from omnigent.spec.types import AgentSpec
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    store = _StubConversationStore()
    store.add(_SESSION)
    conv = store.get_conversation(_SESSION)
    assert conv is not None
    if side_chat:
        # A side chat has no host of its own; its source session owns host and workspace.
        store.add("conv_source")
        conv.labels = {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: "conv_source"}
        conv = store.get_conversation("conv_source")
        assert conv is not None
    conv.host_id, conv.workspace = "host_ws", str(root)
    registry = HostRegistry()
    if host and not remote_host:
        hello = HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name="ws",
            capabilities=[CAP_WORKSPACE_API] if capable else [],
        )
        registry.register("host_ws", object(), hello, owner=None)  # type: ignore[arg-type]
    answering: set[asyncio.Task[None]] = set()

    def tunnel(conn: HostConnection, text: str) -> None:
        call = decode_host_frame(text)
        assert isinstance(call, HostWorkspaceCallFrame)

        async def answer() -> None:
            async for reply in serve_tunnel_call(lambda ws: WorkspaceReader(Path(ws)), call):
                wire = decode_host_frame(encode_host_frame(reply))
                assert isinstance(wire, HostWorkspaceReplyFrame)
                deliver_workspace_reply(conn, wire)

        task = asyncio.get_running_loop().create_task(answer())
        answering.add(task)
        task.add_done_callback(answering.discard)

    monkeypatch.setattr(registry, "send_text", tunnel)
    os_env = OSEnvSpec(sandbox=OSEnvSandboxSpec(type=sandbox) if sandbox else None)
    spec = AgentSpec(spec_version=1, os_env=os_env)
    monkeypatch.setattr(sessions_module, "_load_agent_spec_for_session", lambda *_: spec)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _omnigent_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    if remote_host:
        app.state.host_store = SimpleNamespace(get_host=lambda _host_id: object())
        monkeypatch.setattr(
            "omnigent.server.routes.sessions.routes_resources.host_absent_error",
            lambda _host: WrongReplicaError("host is on another replica", host_id="host_ws"),
        )
    if flag:
        features = {Feature.WORKSPACE_API_SERVER_ACCESS}
        if content_search:
            features.add(Feature.WORKSPACE_CONTENT_SEARCH)
        app.state.feature_flags = FeatureFlags(frozenset(features))
    router = create_sessions_router(
        store,  # type: ignore[arg-type]
        _StubAgentStore(),  # type: ignore[arg-type]
        host_registry=registry,
    )
    app.include_router(router, prefix="/v1")
    return app


ClientFor = Callable[[Path], WorkspaceServiceClient]


@pytest.fixture(
    params=[
        ("host", ProtocolType.CONNECT),
        ("host", ProtocolType.GRPC_WEB),
        ("server", ProtocolType.CONNECT),
        ("server", ProtocolType.GRPC_WEB),
    ],
    ids=["host-connect", "host-grpc-web", "server-connect", "server-grpc-web"],
)
def client_for(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> ClientFor:
    """Return a client for one workspace on one route, over one protocol."""
    route, protocol = request.param

    def make(root: Path) -> WorkspaceServiceClient:
        if route == "server":
            app = _server_app(root, monkeypatch, content_search=True)
            return _connect(app, _SERVER_BASE, protocol)
        reader = WorkspaceReader(root)
        app = WorkspaceServiceASGIApplication(WorkspaceService(lambda _ctx: reader))
        return _connect(app, "http://workspace.test", protocol)

    return make


async def test_list_directory_matches_rest_listing_across_pages(
    tmp_path: Path, client_for: ClientFor
) -> None:
    reader = WorkspaceReader(_workspace(tmp_path))
    client = client_for(tmp_path)

    listed = []
    token = ""
    while True:
        page = await client.list_directory(ListDirectoryRequest(page_size=2, page_token=token))
        assert len(page.entries) <= 2
        listed += [
            (
                e.name,
                e.path,
                "directory" if e.type == ENTRY_TYPE_DIRECTORY else "file",
                e.size if e.HasField("size") else None,
                e.modify_time.seconds,
                e.is_symlink,
            )
            for e in page.entries
        ]
        token = page.next_page_token
        if not token:
            break

    rest = reader.list_or_read("", limit=1000, order="asc")["data"]
    assert listed == [
        (
            e["name"],
            e["path"],
            e["type"],
            e["bytes"],
            e["modified_at"],
            (tmp_path / str(e["path"])).is_symlink(),
        )
        for e in cast("list[dict[str, object]]", rest)
    ]


async def test_read_file_matches_rest_file_content(tmp_path: Path, client_for: ClientFor) -> None:
    reader = WorkspaceReader(_workspace(tmp_path))
    client = client_for(tmp_path)

    paths = [
        "notes.md",
        "café.txt",
        "blob.bin",
        "latin1.txt",
        "big.txt",
        "empty.txt",
        "src/main.py",
    ]
    for path in paths:
        messages = [m async for m in client.read_file(ReadFileRequest(path=path))]
        metadata = messages[0].metadata
        data = b"".join(m.data for m in messages)
        assert messages[0].HasField("metadata") and not messages[0].data, path
        assert not any(m.HasField("metadata") for m in messages[1:]), path
        assert all(len(m.data) <= 64 * 1024 for m in messages), path

        rest = reader.list_or_read(path)
        text = rest["encoding"] == "utf-8"
        content = str(rest["content"])
        assert data == (content.encode() if text else base64.b64decode(content)), path
        assert (metadata.encoding == CONTENT_ENCODING_UTF8) is text, path
        assert metadata.size == rest["bytes"] == len(data), path
        assert metadata.truncated is rest["truncated"], path
        assert metadata.content_type == (rest["content_type"] or ""), path


async def test_list_directory_resumes_after_the_cursor_entry_is_removed(
    tmp_path: Path, client_for: ClientFor
) -> None:
    """An agent may delete files while a client pages; the listing must not restart."""
    client = client_for(_workspace(tmp_path))
    first = await client.list_directory(ListDirectoryRequest(page_size=2))
    (tmp_path / first.entries[-1].path).unlink()

    rest = await client.list_directory(
        ListDirectoryRequest(page_size=1000, page_token=first.next_page_token)
    )

    names = [e.name for e in first.entries] + [e.name for e in rest.entries]
    assert len(names) == len(set(names))
    assert sorted(names) == sorted({e.name for e in first.entries} | set(os.listdir(tmp_path)))


async def test_read_file_flags_a_truncated_stream_like_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_for: ClientFor
) -> None:
    """A cap that splits "é" keeps the text prefix, marked truncated, on every route."""
    monkeypatch.setattr("omnigent.workspace_fs._MAX_READ_BYTES", 2)
    reader = WorkspaceReader(_workspace(tmp_path))
    client = client_for(tmp_path)

    messages = [m async for m in client.read_file(ReadFileRequest(path="café.txt"))]

    metadata = messages[0].metadata
    rest = reader.list_or_read("café.txt")
    assert metadata.truncated is rest["truncated"] is True
    assert metadata.encoding == CONTENT_ENCODING_UTF8
    assert b"".join(m.data for m in messages) == str(rest["content"]).encode() == b"h"


def _workspace_with_escapes(tmp_path: Path) -> Path:
    """A workspace with symlinks that lead out of it, and a FIFO."""
    (tmp_path / "workspace").mkdir()
    root = _workspace(tmp_path / "workspace")
    (tmp_path / "outside.txt").write_text("secret")
    (tmp_path / "outside-link").symlink_to("outside.txt")
    (root / "escape").symlink_to("../outside.txt")
    (root / "up").symlink_to("..")
    os.mkfifo(root / "fifo")
    return root


async def test_stat_path_describes_a_path_as_its_parents_listing_does(
    tmp_path: Path, client_for: ClientFor
) -> None:
    root = _workspace_with_escapes(tmp_path)
    (root / "src" / "inner").symlink_to("main.py")
    client = client_for(root)

    for parent in ("", "src"):
        listed = (await client.list_directory(ListDirectoryRequest(path=parent))).entries
        assert listed
        for entry in listed:
            described = (await client.stat_path(StatPathRequest(path=entry.path))).entry
            assert described == entry, entry.path

    workspace = (await client.stat_path(StatPathRequest(path=""))).entry
    assert (workspace.name, workspace.path, workspace.type) == (
        "workspace",
        "",
        ENTRY_TYPE_DIRECTORY,
    )
    assert not workspace.is_symlink


async def test_read_link_text_returns_a_links_own_text_wherever_it_points(
    tmp_path: Path, client_for: ClientFor
) -> None:
    """Outward and dangling links answer with their text; nothing is followed."""
    root = _workspace_with_escapes(tmp_path)
    client = client_for(root)

    texts = {
        path: (await client.read_link_text(ReadLinkTextRequest(path=path))).target
        for path in ("link.md", "broken", "escape", "up")
    }

    assert texts == {
        "link.md": b"notes.md",
        "broken": b"gone",
        "escape": b"../outside.txt",
        "up": b"..",
    }


@pytest.mark.parametrize(
    ("call", "path", "code"),
    [
        ("stat", "missing.txt", Code.NOT_FOUND),
        ("stat", "../outside.txt", Code.INVALID_ARGUMENT),
        ("stat", "up/outside.txt", Code.INVALID_ARGUMENT),
        ("link", "notes.md", Code.FAILED_PRECONDITION),
        ("link", "src", Code.FAILED_PRECONDITION),
        ("link", "missing", Code.NOT_FOUND),
        ("link", "", Code.INVALID_ARGUMENT),
        ("link", "../outside.txt", Code.INVALID_ARGUMENT),
        # The leaf may point outside, but the path to it may not.
        ("link", "up/outside-link", Code.INVALID_ARGUMENT),
    ],
)
async def test_stat_and_link_refusals_map_to_connect_codes(
    tmp_path: Path, client_for: ClientFor, call: str, path: str, code: Code
) -> None:
    client = client_for(_workspace_with_escapes(tmp_path))

    with pytest.raises(ConnectError) as excinfo:
        if call == "stat":
            await client.stat_path(StatPathRequest(path=path))
        else:
            await client.read_link_text(ReadLinkTextRequest(path=path))
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("call", "path", "code"),
    [
        ("read", "missing.txt", Code.NOT_FOUND),
        ("read", "src", Code.NOT_FOUND),
        ("read", "../outside.txt", Code.INVALID_ARGUMENT),
        ("read", "", Code.INVALID_ARGUMENT),
        ("list", "notes.md", Code.NOT_FOUND),
        ("list", "../", Code.INVALID_ARGUMENT),
    ],
)
async def test_errors_map_to_connect_codes(
    tmp_path: Path, client_for: ClientFor, call: str, path: str, code: Code
) -> None:
    client = client_for(_workspace(tmp_path))

    with pytest.raises(ConnectError) as excinfo:
        if call == "read":
            [m async for m in client.read_file(ReadFileRequest(path=path))]
        else:
            await client.list_directory(ListDirectoryRequest(path=path))
    assert excinfo.value.code == code


@PROTOCOLS
async def test_reader_for_refusal_reaches_the_client(
    tmp_path: Path, protocol: ProtocolType
) -> None:
    def refuse(_ctx: object) -> WorkspaceReader:
        raise ConnectError(Code.PERMISSION_DENIED, "not yours")

    app = WorkspaceServiceASGIApplication(WorkspaceService(refuse))
    client = _connect(app, "http://workspace.test", protocol)

    with pytest.raises(ConnectError) as excinfo:
        [m async for m in client.read_file(ReadFileRequest(path="notes.md"))]
    assert excinfo.value.code == Code.PERMISSION_DENIED


@pytest.mark.parametrize(
    ("query", "fields"),
    [
        ("main", {}),
        ("src", {}),
        ("o", {"include": ["*.{py,md}"]}),
        ("x", {"exclude": ["big.*"]}),
    ],
)
async def test_search_files_matches_the_rest_file_search(
    tmp_path: Path, client_for: ClientFor, query: str, fields: dict[str, Any]
) -> None:
    """With ignored files included, a tree within the walk budget answers as REST does."""
    root = _workspace(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "notes.md", "src"], check=True)
    (root / ".gitignore").write_text("big.txt\n")
    reader = WorkspaceReader(root)
    client = client_for(root)

    response = await client.search_files(
        SearchFilesRequest(
            query=query, mode=FILE_SEARCH_MODE_SUBSTRING, include_git_ignored=True, **fields
        )
    )

    rest = reader.search(
        query,
        include=",".join(fields.get("include", [])) or None,
        exclude=",".join(fields.get("exclude", [])) or None,
    )
    assert [
        (
            r.entry.path,
            "directory" if r.entry.type == ENTRY_TYPE_DIRECTORY else "file",
            r.entry.size if r.entry.HasField("size") else None,
        )
        for r in response.results
    ] == [(e["path"], e["type"], e["bytes"]) for e in cast("list[dict[str, Any]]", rest["data"])]
    assert response.truncated is bool(rest["has_more"])


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"case_sensitive": True},
        {"include": "*.{py,md}", "exclude": "big.txt"},
        {"maximum_results": 1},
    ],
)
async def test_search_text_matches_the_rest_content_search(
    tmp_path: Path, client_for: ClientFor, fields: dict[str, Any]
) -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.os_env import create_os_environment
    from omnigent.runner.content_search import search_content

    root = _workspace(tmp_path)
    (root / "src" / "more.py").write_text("Title = 'title'\nnothing\n")
    client = client_for(root)
    # Unconfined, as the host's workspaces are.
    spec = OSEnvSpec(type="caller_process", cwd=str(root), sandbox=OSEnvSandboxSpec(type="none"))
    environment = create_os_environment(spec)
    assert environment is not None

    try:
        responses = [
            m async for m in client.search_text(SearchTextRequest(query="title", **fields))
        ]
        rest = await search_content(
            environment,
            "title",
            case_sensitive=fields.get("case_sensitive", False),
            include=fields.get("include", ""),
            exclude=fields.get("exclude", ""),
            limit=fields.get("maximum_results", 500),
        )
    finally:
        environment.close()

    matches = [
        {
            "path": m.path,
            "line": m.line,
            "column": m.column,
            "text": m.text,
            "match_start": m.match_start,
            "match_end": m.match_end,
        }
        for response in responses
        for m in response.matches
    ]
    assert rest["matches"]
    if "maximum_results" in fields:
        # ripgrep searches files in parallel, so which match comes first varies.
        assert len(matches) == len(rest["matches"])
    else:
        assert sorted(matches, key=str) == sorted(rest["matches"], key=str)
    assert responses[-1].done and not any(r.done for r in responses[:-1])
    assert responses[-1].truncated is rest["truncated"]
    assert (responses[-1].warning or None) == rest["warning"]


async def test_base_reads_match_file_reads_and_changes_list_on_every_route(
    tmp_path: Path, client_for: ClientFor
) -> None:
    """An unchanged file reads the same at HEAD as from the disk; a change lists with its blob."""
    root = _workspace(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    for argv in (
        ["init", "-qb", "main"],
        ["add", "notes.md", "big.txt", "src"],
        ["commit", "-qm", "c"],
    ):
        subprocess.run(["git", "-C", str(root), *argv], check=True, env=env)
    (root / "src" / "main.py").write_text("print('changed')\n")
    client = client_for(root)

    changes = await client.list_working_copy_changes(ListWorkingCopyChangesRequest())
    modified = next(f for f in changes.files if f.path == "src/main.py")
    listed = await client.list_repository_refs(ListRepositoryRefsRequest())
    head = changes.base.head
    tree = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "--object-only", head, "big.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base = [
        m
        async for m in client.read_base_file(
            ReadBaseFileRequest(commit=head, path="big.txt", object=tree)
        )
    ]
    disk = [m async for m in client.read_file(ReadFileRequest(path="big.txt"))]

    # Untracked files are listed as added; the committed ones that didn't change aren't.
    assert {"src/main.py", "café.txt"} <= {f.path for f in changes.files}
    assert not {"notes.md", "big.txt"} & {f.path for f in changes.files}
    assert modified.status == CHANGE_STATUS_MODIFIED
    assert modified.base_object and modified.HasField("working")
    assert listed.refs and not listed.truncated
    context = await client.resolve_repository_context(ResolveRepositoryContextRequest())
    assert context.branch == "main" and context.head_sha == head
    assert b"".join(m.data for m in base) == b"".join(m.data for m in disk)
    assert base[0].metadata.size == disk[0].metadata.size
    assert base[0].metadata.encoding == disk[0].metadata.encoding
    assert base[0].metadata.content_type == disk[0].metadata.content_type


async def test_capabilities_list_the_served_methods(tmp_path: Path, client_for: ClientFor) -> None:
    client = client_for(tmp_path)

    response = await client.get_capabilities(GetCapabilitiesRequest())

    # SearchText is listed exactly where ripgrep is installed.
    assert set(response.capabilities) ^ ({"SearchText"} - _WITH_RG) == {
        "ListDirectory",
        "ReadFile",
        "StatPath",
        "ReadLinkText",
        "SearchFiles",
        "search_files.substring",
        "SearchText",
        "ResolveWorkingCopyBase",
        "revision_comparison",
        "ListWorkingCopyChanges",
        "ReadBaseFile",
        "ListRepositoryRefs",
        "ListBranchCommits",
        "branch_commits",
        "ResolveRepositoryContext",
    }


async def test_capabilities_leave_out_methods_whose_tool_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.host.workspace_service.shutil.which", lambda _name: None)
    app = WorkspaceServiceASGIApplication(WorkspaceService(lambda _ctx: WorkspaceReader(tmp_path)))
    client = _connect(app, "http://workspace.test", ProtocolType.CONNECT)

    response = await client.get_capabilities(GetCapabilitiesRequest())

    assert set(response.capabilities) == {"ListDirectory", "ReadFile", "StatPath", "ReadLinkText"}


async def test_a_search_mode_not_served_answers_unimplemented(
    tmp_path: Path, client_for: ClientFor
) -> None:
    """A client that skips GetCapabilities still gets a code it can fall back on."""
    client = client_for(tmp_path)

    with pytest.raises(ConnectError) as fuzzy:
        await client.search_files(SearchFilesRequest(query="main"))  # fuzzy, the default

    assert fuzzy.value.code == Code.UNIMPLEMENTED


@PROTOCOLS
@pytest.mark.parametrize(
    ("broken", "code"),
    [
        ({"host": False}, Code.UNAVAILABLE),
        ({"capable": False}, Code.UNIMPLEMENTED),
        ({"sandbox": None}, Code.FAILED_PRECONDITION),
        ({"flag": False}, Code.UNIMPLEMENTED),  # the route 404s; Connect reads that so
    ],
    ids=["host-offline", "host-outdated", "confined-session", "flag-off"],
)
async def test_server_route_refuses_with_a_connect_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: ProtocolType,
    broken: dict[str, Any],
    code: Code,
) -> None:
    """Clients see a typed error on either protocol, so they can fall back to REST."""
    client = _connect(_server_app(tmp_path, monkeypatch, **broken), _SERVER_BASE, protocol)

    with pytest.raises(ConnectError) as excinfo:
        [m async for m in client.read_file(ReadFileRequest(path="notes.md"))]
    assert excinfo.value.code == code


@PROTOCOLS
async def test_server_route_serves_a_side_chat_from_its_source_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: ProtocolType
) -> None:
    app = _server_app(_workspace(tmp_path), monkeypatch, side_chat=True)
    client = _connect(app, _SERVER_BASE, protocol)

    page = await client.list_directory(ListDirectoryRequest())

    assert "notes.md" in {e.name for e in page.entries}


@pytest.mark.parametrize("content_search", [True, False], ids=["flag-on", "flag-off"])
async def test_server_route_serves_text_search_only_with_its_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_search: bool
) -> None:
    """With its flag off, SearchText is refused and unlisted even when the host serves it."""
    reached: list[str] = []

    async def search_text(
        _self: WorkspaceService, _request: SearchTextRequest, _ctx: object
    ) -> AsyncIterator[SearchTextResponse]:
        reached.append("SearchText")
        yield SearchTextResponse(done=True)

    monkeypatch.setitem(_SERVED, "SearchText", _Served())
    monkeypatch.setattr(WorkspaceService, "search_text", search_text)
    app = _server_app(_workspace(tmp_path), monkeypatch, content_search=content_search)
    client = _connect(app, _SERVER_BASE, ProtocolType.CONNECT)

    capabilities = set((await client.get_capabilities(GetCapabilitiesRequest())).capabilities)
    if content_search:
        searched = [m async for m in client.search_text(SearchTextRequest(query="q"))]
        assert searched[-1].done
    else:
        with pytest.raises(ConnectError) as refused:
            [m async for m in client.search_text(SearchTextRequest(query="q"))]
        assert refused.value.code == Code.UNIMPLEMENTED

    assert ("SearchText" in capabilities) is content_search
    assert reached == (["SearchText"] if content_search else [])


@pytest.mark.parametrize(
    "method",
    ["Bogus", "list_directory", "__init__", "_reader_for", "GetInfo", "OpenWorkspace"],
)
async def test_tunnel_dispatches_only_methods_in_the_table(tmp_path: Path, method: str) -> None:
    """A method name from the wire never selects an attribute the table doesn't list."""
    frame = HostWorkspaceCallFrame(
        request_id="r1", workspace=str(tmp_path), method=method, request=b""
    )

    replies = [
        reply async for reply in serve_tunnel_call(lambda ws: WorkspaceReader(Path(ws)), frame)
    ]

    assert [(r.done, r.error_code) for r in replies] == [(True, Code.UNIMPLEMENTED.value)]


async def test_server_route_readdresses_a_host_on_another_replica(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REST wrong-replica error lets the client's fetch re-address, as for file reads."""
    app = _server_app(tmp_path, monkeypatch, remote_host=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as http:
        response = await http.post(
            f"/v1/sessions/{_SESSION}/workspace"
            "/omnigent.api.workspace.v1alpha1.WorkspaceService/ListDirectory",
            json={},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wrong_replica"


async def test_host_disconnect_ends_a_waiting_call(tmp_path: Path) -> None:
    """A call waiting on a host that drops gets an error instead of waiting out its timeout."""
    from omnigent.server.workspace_api import HostTarget, HostWorkspaceService

    registry = HostRegistry()
    hello = HostHelloFrame(version="t", frame_protocol_version=1, name="ws")
    conn = registry.register("host_ws", object(), hello, owner=None)  # type: ignore[arg-type]
    registry.send_text = lambda *_: None  # type: ignore[method-assign]
    service = HostWorkspaceService(HostTarget(registry, conn, str(tmp_path)), content_search=False)

    call = asyncio.create_task(service.list_directory(ListDirectoryRequest(), None))  # type: ignore[arg-type]
    await poll_until_async(lambda: conn.pending_workspace_calls, timeout_s=5)
    registry.deregister("host_ws", conn=conn)

    with pytest.raises(ConnectError) as excinfo:
        await call
    assert excinfo.value.code == Code.UNAVAILABLE


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
async def test_socket_serves_only_valid_tokens_for_this_host(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _workspace(workspace)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    listener = await start_workspace_socket(lambda ws: WorkspaceReader(Path(ws)), host_id="host_a")
    assert listener is not None
    # The host's own shutdown handlers must stay in charge while the socket runs.
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    key = listener.token_key

    def bearer(
        signing_key: str = key,
        host_id: str = "host_a",
        root: str = str(workspace),
        now: float | None = None,
    ) -> str:
        token, _ = mint_token(
            signing_key, host_id=host_id, session_id="s1", workspace=root, now=now
        )
        return f"Bearer {token}"

    try:
        # Only this user may enter the socket's directory or open the socket.
        assert stat.S_IMODE(listener.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(listener.path.stat().st_mode) == 0o600
        transport = httpx.AsyncHTTPTransport(uds=str(listener.path))
        async with httpx.AsyncClient(transport=transport, base_url="http://workspace") as http:

            async def list_root(authorization: str | None) -> httpx.Response:
                headers = {"Authorization": authorization} if authorization else {}
                return await http.post(
                    "/omnigent.api.workspace.v1alpha1.WorkspaceService/ListDirectory",
                    json={},
                    headers=headers,
                )

            allowed = await list_root(bearer())
            assert allowed.status_code == 200, allowed.text
            assert {"notes.md", "src"} <= {e["name"] for e in allowed.json()["entries"]}

            refused = {
                "no token": None,
                "not a token": "Bearer not-a-token",
                "another host's token": bearer(host_id="host_b"),
                "another key": bearer(signing_key="another-host-process-key-0123456789"),
                "expired": bearer(now=time.time() - 3600),
            }
            for case, authorization in refused.items():
                response = await list_root(authorization)
                assert response.status_code == 401, case
                assert response.json()["code"] == "unauthenticated", case

            missing = await list_root(bearer(root=str(tmp_path / "gone")))
            assert missing.status_code == 404
    finally:
        await listener.stop()

    assert not listener.path.parent.exists()


async def test_socket_that_cannot_be_prepared_leaves_the_host_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full temp disk or a refused bind turns direct access off instead of failing startup."""

    def no_space(*_args: object, **_kwargs: object) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("omnigent.host.workspace_service.tempfile.mkdtemp", no_space)

    assert await start_workspace_socket(lambda ws: WorkspaceReader(Path(ws)), host_id="h") is None
