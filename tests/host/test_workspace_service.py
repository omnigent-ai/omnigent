"""Parity tests for the workspace API, on every route that serves it.

The API must answer exactly what the REST file routes answer, because both
read through :class:`WorkspaceReader`. Each parity test runs the real Connect
client on both routes (the host's service directly, and the server forwarding
over an in-memory host tunnel) and both browser-capable protocols, so neither
a route nor a protocol can change an answer. The ``helper`` cases answer through
the host's workspace helper instead: ``$OMNIGENT_WORKSPACE_HELPER`` when it names
a binary, else the fake in ``tests/_helpers/fake_workspace_helper.py``. The
reader's own input matrix lives in ``tests/test_workspace_fs.py``; the helper's
lifecycle, in ``test_workspace_helper.py``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import socket
import stat
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
    CONTENT_ENCODING_UTF8,
    ENTRY_TYPE_DIRECTORY,
    GetCapabilitiesRequest,
    ListDirectoryRequest,
    ReadFileRequest,
    SearchFilesRequest,
    SearchTextRequest,
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
from omnigent.host.workspace_helper.proxy import WorkspaceHelperProxy
from omnigent.host.workspace_service import (
    WorkspaceService,
    serve_tunnel_call,
    start_workspace_socket,
)
from omnigent.host.workspace_tokens import mint_token
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostConnection, HostRegistry, deliver_workspace_reply
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.fake_workspace_helper import relax_helper_deadlines, write_fake_helper
from tests._helpers.polling import poll_until_async
from tests.server.routes.test_shell_permission_gate import _StubAgentStore, _StubConversationStore

pytestmark = pytest.mark.asyncio

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
    helper: WorkspaceHelperProxy | None = None,
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
            reader_for = lambda ws: WorkspaceReader(Path(ws))  # noqa: E731
            async for reply in serve_tunnel_call(reader_for, call, helper):
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


_PYTHON_ROUTES = {
    "host-connect": ("host", ProtocolType.CONNECT, False),
    "host-grpc-web": ("host", ProtocolType.GRPC_WEB, False),
    "server-connect": ("server", ProtocolType.CONNECT, False),
    "server-grpc-web": ("server", ProtocolType.GRPC_WEB, False),
}
_ROUTES = {
    **_PYTHON_ROUTES,
    "host-helper": ("host", ProtocolType.CONNECT, True),
    "server-helper": ("server", ProtocolType.CONNECT, True),
}


async def _clients(
    route: str,
    protocol: ProtocolType,
    through_helper: bool,
    monkeypatch: pytest.MonkeyPatch,
    helper_directory: Callable[[], Path],
) -> AsyncIterator[ClientFor]:
    """Yield a factory of clients for one route, protocol and backend."""
    helper: WorkspaceHelperProxy | None = None
    if through_helper:
        if os.name != "posix":
            pytest.skip("the helper inherits a POSIX socket fd")
        relax_helper_deadlines(monkeypatch)
        binary = os.environ.get("OMNIGENT_WORKSPACE_HELPER") or str(
            write_fake_helper(helper_directory())
        )
        helper = WorkspaceHelperProxy(binary)
        # A helper that didn't start would leave the Python reader answering unnoticed.
        assert "ListDirectory" in await helper.capabilities()

    def make(root: Path) -> WorkspaceServiceClient:
        if route == "server":
            return _connect(_server_app(root, monkeypatch, helper=helper), _SERVER_BASE, protocol)
        reader = WorkspaceReader(root)
        app = WorkspaceServiceASGIApplication(WorkspaceService(lambda _ctx: reader, helper))
        return _connect(app, "http://workspace.test", protocol)

    try:
        yield make
    finally:
        if helper is not None:
            await helper.close()


@pytest.fixture(params=list(_ROUTES.values()), ids=list(_ROUTES))
async def client_for(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> AsyncIterator[ClientFor]:
    """Return a client for one workspace on every route, protocol and backend."""
    async for make in _clients(
        *request.param, monkeypatch, lambda: tmp_path_factory.mktemp("helper")
    ):
        yield make
    # The Python reader answering instead of the helper would pass parity unnoticed.
    fallbacks = [
        record.getMessage()
        for record in caplog.get_records("call")
        if "so the host's reader did" in record.getMessage()
    ]
    assert fallbacks == []


@pytest.fixture(params=list(_PYTHON_ROUTES.values()), ids=list(_PYTHON_ROUTES))
async def python_client_for(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[ClientFor]:
    """Like ``client_for``, for behavior of the host's own Python reader."""
    async for make in _clients(
        *request.param, monkeypatch, lambda: tmp_path_factory.mktemp("helper")
    ):
        yield make


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
            )
            for e in page.entries
        ]
        token = page.next_page_token
        if not token:
            break

    rest = reader.list_or_read("", limit=1000, order="asc")["data"]
    assert listed == [
        (e["name"], e["path"], e["type"], e["bytes"], e["modified_at"])
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_client_for: ClientFor
) -> None:
    """A cap that splits "é" keeps the text prefix, marked truncated, on every route."""
    monkeypatch.setattr("omnigent.workspace_fs._MAX_READ_BYTES", 2)
    reader = WorkspaceReader(_workspace(tmp_path))
    client = python_client_for(tmp_path)

    messages = [m async for m in client.read_file(ReadFileRequest(path="café.txt"))]

    metadata = messages[0].metadata
    rest = reader.list_or_read("café.txt")
    assert metadata.truncated is rest["truncated"] is True
    assert metadata.encoding == CONTENT_ENCODING_UTF8
    assert b"".join(m.data for m in messages) == str(rest["content"]).encode() == b"h"


@pytest.mark.parametrize(
    ("call", "path", "code"),
    [
        ("read", "missing.txt", Code.NOT_FOUND),
        ("read", "src", Code.NOT_FOUND),
        ("read", "../outside.txt", Code.INVALID_ARGUMENT),
        ("read", "", Code.INVALID_ARGUMENT),
        ("read", "escape", Code.INVALID_ARGUMENT),
        ("read", "fifo", Code.NOT_FOUND),
        ("list", "notes.md", Code.NOT_FOUND),
        ("list", "../", Code.INVALID_ARGUMENT),
        ("list", "up", Code.INVALID_ARGUMENT),
    ],
)
async def test_errors_map_to_connect_codes(
    tmp_path: Path, client_for: ClientFor, call: str, path: str, code: Code
) -> None:
    (tmp_path / "workspace").mkdir()
    root = _workspace(tmp_path / "workspace")
    (tmp_path / "outside.txt").write_text("secret")
    # Symlinks leading out of the workspace, and a FIFO no read may block on.
    (root / "escape").symlink_to("../outside.txt")
    (root / "up").symlink_to("..")
    os.mkfifo(root / "fifo")
    client = client_for(root)

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


async def test_capabilities_list_the_served_methods(
    tmp_path: Path, python_client_for: ClientFor
) -> None:
    client = python_client_for(tmp_path)

    response = await client.get_capabilities(GetCapabilitiesRequest())

    assert set(response.capabilities) == {"ListDirectory", "ReadFile"}


async def test_methods_not_served_yet_answer_unimplemented(
    tmp_path: Path, python_client_for: ClientFor
) -> None:
    """A client that skips GetCapabilities still gets a code it can fall back on."""
    client = python_client_for(tmp_path)

    with pytest.raises(ConnectError) as unary:
        await client.search_files(SearchFilesRequest(query="main"))
    with pytest.raises(ConnectError) as streamed:
        [m async for m in client.search_text(SearchTextRequest(query="main"))]

    assert unary.value.code == streamed.value.code == Code.UNIMPLEMENTED


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


@pytest.mark.skipif(os.name != "posix", reason="the helper inherits a POSIX socket fd")
@pytest.mark.parametrize("content_search", [True, False], ids=["flag-on", "flag-off"])
async def test_server_route_serves_text_search_only_with_its_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_search: bool
) -> None:
    """Every helper method reaches the host, except SearchText while its flag is off."""
    (tmp_path / "workspace").mkdir()
    root = _workspace(tmp_path / "workspace")
    relax_helper_deadlines(monkeypatch)
    helper = WorkspaceHelperProxy(str(write_fake_helper(tmp_path)))
    try:
        app = _server_app(root, monkeypatch, helper=helper, content_search=content_search)
        client = _connect(app, _SERVER_BASE, ProtocolType.CONNECT)

        capabilities = set((await client.get_capabilities(GetCapabilitiesRequest())).capabilities)
        stat = await client.stat_path(StatPathRequest(path="notes.md"))
        if content_search:
            searched = [m async for m in client.search_text(SearchTextRequest(query="q"))]
            assert searched[-1].done
        else:
            with pytest.raises(ConnectError) as refused:
                [m async for m in client.search_text(SearchTextRequest(query="q"))]
            assert refused.value.code == Code.UNIMPLEMENTED
    finally:
        await helper.close()

    assert stat.entry.name == "workspace"
    assert ("SearchText" in capabilities) is content_search
    assert "StatPath" in capabilities


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
    service = HostWorkspaceService(HostTarget(registry, conn, str(tmp_path)))

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
