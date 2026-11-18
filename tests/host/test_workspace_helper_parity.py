"""The Rust workspace helper answers the workspace API exactly as the host does.

Each case sends one call to the helper executable and to the host's Python
:class:`~omnigent.host.workspace_service.WorkspaceService` over the same
directory, and compares the answers message for message, or the error codes.
The helper leaves ``content_type`` for the host to fill in, so it is compared
empty, and its additions the host doesn't make (``is_symlink``, ``ignored``,
the file's ``modify_time``) are cleared first. The tests need a built helper:
``OMNIGENT_WORKSPACE_HELPER=<path>``, or ``cargo build --manifest-path
rust/Cargo.toml``; otherwise they skip.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from connectrpc.errors import ConnectError
from google.protobuf.message import Message

from omnigent.api.workspace.v1alpha1.workspace_helper_pb2 import (
    OpenWorkspaceRequest,
    OpenWorkspaceResponse,
)
from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    ListDirectoryRequest,
    ListDirectoryResponse,
    ReadFileRequest,
    ReadFileResponse,
)
from omnigent.host.workspace_service import WorkspaceService
from omnigent.workspace_fs import _MAX_READ_BYTES, WorkspaceReader

grpc = pytest.importorskip("grpc")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform == "win32", reason="the helper serves Unix sockets"),
]

_REPO = Path(__file__).resolve().parents[2]
_SERVICE = "/omnigent.api.workspace.v1alpha1.WorkspaceService"
# A FIFO read that blocks would hang the call; both sides must answer well within this.
_CALL_TIMEOUT_S = 10.0
# grpcio names a Unix socket's path as the authority, which HTTP/2 servers refuse.
_CHANNEL_OPTIONS = (("grpc.default_authority", "localhost"),)


def _helper_binary() -> Path | None:
    configured = os.environ.get("OMNIGENT_WORKSPACE_HELPER")
    if configured:
        return Path(configured)
    built = _REPO / "rust" / "target" / "debug" / "omnigent-workspace-helper"
    return built if built.exists() else None


class _Helper:
    """The helper executable on a private socket, with one workspace open."""

    def __init__(self, channel: grpc.Channel, workspace_id: str) -> None:
        self._channel = channel
        self._metadata = (("omnigent-workspace-id", workspace_id),)

    def list_directory(self, request: ListDirectoryRequest) -> list[Message]:
        call = self._channel.unary_unary(
            f"{_SERVICE}/ListDirectory",
            request_serializer=ListDirectoryRequest.SerializeToString,
            response_deserializer=ListDirectoryResponse.FromString,
        )
        return [call(request, metadata=self._metadata, timeout=_CALL_TIMEOUT_S)]

    def read_file(self, request: ReadFileRequest) -> list[Message]:
        call = self._channel.unary_stream(
            f"{_SERVICE}/ReadFile",
            request_serializer=ReadFileRequest.SerializeToString,
            response_deserializer=ReadFileResponse.FromString,
        )
        return list(call(request, metadata=self._metadata, timeout=_CALL_TIMEOUT_S))


def _open_workspace(channel: grpc.Channel, root: Path) -> str:
    call = channel.unary_unary(
        "/omnigent.api.workspace.v1alpha1.WorkspaceHelperService/OpenWorkspace",
        request_serializer=OpenWorkspaceRequest.SerializeToString,
        response_deserializer=OpenWorkspaceResponse.FromString,
    )
    return call(OpenWorkspaceRequest(root=str(root)), timeout=_CALL_TIMEOUT_S).workspace_id


def _without_additions(responses: list[Message]) -> list[Message]:
    """The helper's answer with the fields only it fills cleared."""
    for response in responses:
        if isinstance(response, ListDirectoryResponse):
            for entry in response.entries:
                entry.is_symlink = False
                entry.ignored = False
        elif isinstance(response, ReadFileResponse) and response.HasField("metadata"):
            response.metadata.ClearField("modify_time")
    return responses


@pytest.fixture(scope="module")
def helper_process(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    binary = _helper_binary()
    if binary is None:
        pytest.skip("no workspace helper build; set OMNIGENT_WORKSPACE_HELPER")
    socket_path = tmp_path_factory.mktemp("helper") / "helper.sock"
    process = subprocess.Popen(
        [str(binary), "--socket", str(socket_path), "--exit-on-stdin-eof"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    assert ready["ready"] is True
    try:
        yield socket_path
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=10)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A workspace with every kind of entry the host reader distinguishes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    (tmp_path / "outside").mkdir()
    (workspace / "notes.md").write_text("# Title\nbody\n")
    (workspace / "café.txt").write_text("héllo")
    (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    (workspace / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
    (workspace / "big.txt").write_text("x" * 200_000)
    (workspace / "empty.txt").write_bytes(b"")
    # The cap splits "é": the tail is dropped and the file stays text.
    (workspace / "split.txt").write_bytes(b"x" * (_MAX_READ_BYTES - 1) + "é tail".encode())
    oversize_binary = bytearray(b"x" * (_MAX_READ_BYTES + 5))
    oversize_binary[10] = 0xFF
    (workspace / "oversize.bin").write_bytes(bytes(oversize_binary))
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "main.py").write_text("print('hi')\n")
    for index in range(5):
        (workspace / "many").mkdir(exist_ok=True)
        (workspace / "many" / f"f{index}").write_text(str(index))
    (workspace / "link.md").symlink_to("notes.md")
    (workspace / "linked-src").symlink_to("src")
    (workspace / "broken").symlink_to("gone")
    (workspace / "out-file").symlink_to(tmp_path / "outside.txt")
    (workspace / "out-dir").symlink_to(tmp_path / "outside")
    (workspace / "device").symlink_to("/dev/null")
    os.mkfifo(workspace / "fifo")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(workspace / "socket"))
    listener.close()
    os.close(os.open(workspace / "\udcff-not-utf8", os.O_CREAT | os.O_WRONLY))
    return workspace


async def _host_answer(
    service: WorkspaceService, method: str, request: Message
) -> list[Message] | str:
    try:
        if method == "ListDirectory":
            assert isinstance(request, ListDirectoryRequest)
            return [await service.list_directory(request, None)]
        assert isinstance(request, ReadFileRequest)
        responses: AsyncIterator[ReadFileResponse] = service.read_file(request, None)
        answers: list[Message] = []
        async for response in responses:
            # The helper leaves the MIME type to the host.
            if response.HasField("metadata"):
                response.metadata.content_type = ""
            answers.append(response)
        return answers
    except ConnectError as exc:
        return exc.code.name


def _helper_answer(helper: _Helper, method: str, request: Message) -> list[Message] | str:
    try:
        if method == "ListDirectory":
            assert isinstance(request, ListDirectoryRequest)
            return _without_additions(helper.list_directory(request))
        assert isinstance(request, ReadFileRequest)
        return _without_additions(helper.read_file(request))
    except grpc.RpcError as exc:
        return exc.code().name


@pytest.fixture
def both(helper_process: Path, root: Path) -> Iterator[tuple[WorkspaceService, _Helper]]:
    reader = WorkspaceReader(root)
    with grpc.insecure_channel(f"unix:{helper_process}", options=_CHANNEL_OPTIONS) as channel:
        helper = _Helper(channel, _open_workspace(channel, root.resolve()))
        yield WorkspaceService(lambda _ctx: reader), helper


_LISTINGS = [
    "",
    ".",
    "./src//",
    "src/..",
    "src",
    "linked-src",
    "many",
    "out-dir",
    "../workspace",
    "/etc",
    "missing",
    "notes.md",
    "fifo",
    "a\0b",
]


@pytest.mark.parametrize("path", _LISTINGS)
async def test_list_directory_matches_the_host(
    both: tuple[WorkspaceService, _Helper], path: str
) -> None:
    service, helper = both
    request = ListDirectoryRequest(path=path)
    expected = await _host_answer(service, "ListDirectory", request)
    actual = await asyncio.to_thread(_helper_answer, helper, "ListDirectory", request)
    assert actual == expected


@pytest.mark.parametrize(
    ("page_size", "page_token"),
    [(2, ""), (2, "many/f1"), (2, "many/f1x"), (10, "many/f4"), (0, ""), (5000, ""), (-1, "")],
)
async def test_list_directory_pages_match_the_host(
    both: tuple[WorkspaceService, _Helper], page_size: int, page_token: str
) -> None:
    service, helper = both
    request = ListDirectoryRequest(path="many", page_size=page_size, page_token=page_token)
    expected = await _host_answer(service, "ListDirectory", request)
    actual = await asyncio.to_thread(_helper_answer, helper, "ListDirectory", request)
    assert actual == expected


_READS = [
    "notes.md",
    "café.txt",
    "blob.bin",
    "latin1.txt",
    "big.txt",
    "empty.txt",
    "split.txt",
    "oversize.bin",
    "./src/../src/main.py",
    "link.md",
    "linked-src/main.py",
    "broken",
    "out-file",
    "out-dir/anything",
    "device",
    "fifo",
    "socket",
    "src",
    "missing.txt",
    "",
    "../outside.txt",
    "/etc/hosts",
    "a\0b",
]


@pytest.mark.parametrize("path", _READS)
async def test_read_file_matches_the_host(
    both: tuple[WorkspaceService, _Helper], path: str
) -> None:
    service, helper = both
    request = ReadFileRequest(path=path)
    expected = await _host_answer(service, "ReadFile", request)
    actual = await asyncio.to_thread(_helper_answer, helper, "ReadFile", request)
    assert actual == expected


async def test_calls_without_an_open_workspace_are_refused(
    helper_process: Path, root: Path
) -> None:
    with grpc.insecure_channel(f"unix:{helper_process}", options=_CHANNEL_OPTIONS) as channel:
        unpinned = _Helper(channel, "")
        forged = _Helper(channel, "0" * 32)
        request = ReadFileRequest(path="notes.md")
        assert _helper_answer(unpinned, "ReadFile", request) == "FAILED_PRECONDITION"
        assert _helper_answer(forged, "ReadFile", request) == "NOT_FOUND"
