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
import signal
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
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
    WorkspaceService,
    serve_tunnel_call,
    start_workspace_socket,
)
from omnigent.host.workspace_tokens import mint_token
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostConnection, HostRegistry, deliver_workspace_reply
from omnigent.workspace_fs import WorkspaceReader
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
) -> FastAPI:
    """The sessions API, with the session's host answering over an in-memory tunnel.

    Each keyword can break one precondition of the server route.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import create_sessions_router
    from omnigent.spec.types import AgentSpec

    store = _StubConversationStore()
    store.add(_SESSION)
    conv = store.get_conversation(_SESSION)
    assert conv is not None
    conv.host_id, conv.workspace = "host_ws", str(root)
    registry = HostRegistry()
    if host:
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
    if flag:
        app.state.feature_flags = FeatureFlags(frozenset({Feature.WORKSPACE_API}))
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
            return _connect(_server_app(root, monkeypatch), _SERVER_BASE, protocol)
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


async def test_capabilities_list_the_served_methods(tmp_path: Path, client_for: ClientFor) -> None:
    client = client_for(tmp_path)

    response = await client.get_capabilities(GetCapabilitiesRequest())

    assert set(response.capabilities) == {"ListDirectory", "ReadFile"}


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


async def test_host_disconnect_ends_a_waiting_call(tmp_path: Path) -> None:
    """A call waiting on a host that drops gets an error instead of waiting out its timeout."""
    from omnigent.server.workspace_api import HostTarget, HostWorkspaceService

    registry = HostRegistry()
    hello = HostHelloFrame(version="t", frame_protocol_version=1, name="ws")
    conn = registry.register("host_ws", object(), hello, owner=None)  # type: ignore[arg-type]
    registry.send_text = lambda *_: None  # type: ignore[method-assign]
    service = HostWorkspaceService(HostTarget(registry, conn, str(tmp_path)))

    call = asyncio.create_task(service.list_directory(ListDirectoryRequest(), None))  # type: ignore[arg-type]
    while not conn.pending_workspace_calls:
        await asyncio.sleep(0)
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
