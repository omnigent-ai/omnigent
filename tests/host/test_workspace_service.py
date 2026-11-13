"""Parity tests for the workspace API served by the host.

The API must answer exactly what the REST file routes answer, because both
read through :class:`WorkspaceReader`. Each test calls the service with the
real Connect client over every protocol a route may use, so a transport
can't change an answer. The reader's own input matrix lives in
``tests/test_workspace_fs.py``.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import cast

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.protocol import ProtocolType
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
from omnigent.host.workspace_service import WorkspaceService
from omnigent.workspace_fs import WorkspaceReader

pytestmark = pytest.mark.asyncio

PROTOCOLS = pytest.mark.parametrize("protocol", [ProtocolType.CONNECT, ProtocolType.GRPC_WEB])


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


def _client(reader: WorkspaceReader, protocol: ProtocolType) -> WorkspaceServiceClient:
    app = WorkspaceServiceASGIApplication(WorkspaceService(lambda _ctx: reader))
    return WorkspaceServiceClient(
        "http://workspace.test", protocol=protocol, http_client=Client(ASGITransport(app))
    )


@PROTOCOLS
async def test_list_directory_matches_rest_listing_across_pages(
    tmp_path: Path, protocol: ProtocolType
) -> None:
    reader = WorkspaceReader(_workspace(tmp_path))
    client = _client(reader, protocol)

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


@PROTOCOLS
async def test_read_file_matches_rest_file_content(tmp_path: Path, protocol: ProtocolType) -> None:
    root = _workspace(tmp_path)
    reader = WorkspaceReader(root)
    client = _client(reader, protocol)

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


@PROTOCOLS
async def test_list_directory_resumes_after_the_cursor_entry_is_removed(
    tmp_path: Path, protocol: ProtocolType
) -> None:
    """An agent may delete files while a client pages; the listing must not restart."""
    client = _client(WorkspaceReader(_workspace(tmp_path)), protocol)
    first = await client.list_directory(ListDirectoryRequest(page_size=2))
    (tmp_path / first.entries[-1].path).unlink()

    rest = await client.list_directory(
        ListDirectoryRequest(page_size=1000, page_token=first.next_page_token)
    )

    names = [e.name for e in first.entries] + [e.name for e in rest.entries]
    assert len(names) == len(set(names))
    assert sorted(names) == sorted({e.name for e in first.entries} | set(os.listdir(tmp_path)))


async def test_read_file_flags_a_truncated_stream_like_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap that splits "é" keeps the text prefix, marked truncated, on both routes."""
    monkeypatch.setattr("omnigent.workspace_fs._MAX_READ_BYTES", 2)
    reader = WorkspaceReader(_workspace(tmp_path))
    client = _client(reader, ProtocolType.CONNECT)

    messages = [m async for m in client.read_file(ReadFileRequest(path="café.txt"))]

    metadata = messages[0].metadata
    rest = reader.list_or_read("café.txt")
    assert metadata.truncated is rest["truncated"] is True
    assert metadata.encoding == CONTENT_ENCODING_UTF8
    assert b"".join(m.data for m in messages) == str(rest["content"]).encode() == b"h"


@PROTOCOLS
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
    tmp_path: Path, protocol: ProtocolType, call: str, path: str, code: Code
) -> None:
    client = _client(WorkspaceReader(_workspace(tmp_path)), protocol)

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
    client = WorkspaceServiceClient(
        "http://workspace.test", protocol=protocol, http_client=Client(ASGITransport(app))
    )

    with pytest.raises(ConnectError) as excinfo:
        [m async for m in client.read_file(ReadFileRequest(path="notes.md"))]
    assert excinfo.value.code == Code.PERMISSION_DENIED


async def test_capabilities_list_the_served_methods(tmp_path: Path) -> None:
    client = _client(WorkspaceReader(tmp_path), ProtocolType.CONNECT)

    response = await client.get_capabilities(GetCapabilitiesRequest())

    assert set(response.capabilities) == {"ListDirectory", "ReadFile"}
