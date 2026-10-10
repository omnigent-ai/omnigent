"""Tests for the filesystem download route when the file changes mid-stream.

``_fs_download`` announces ``Content-Length`` from the size at open time and
then streams chunks from the open descriptor. If the file shrinks underneath
it, the read returns empty before the announced length is reached; the
transfer must then fail visibly (and be logged) rather than end as a clean,
silently truncated response.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import BinaryIO

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner import create_runner_app
from omnigent.runner.environment_filesystem import CallerProcessFilesystem
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests.runner.helpers import NullServerClient

_CHUNK = 64 * 1024
# Three full chunks, so the stream has already sent data when the file shrinks.
_SIZE = 3 * _CHUNK
_DOWNLOAD_URL = (
    f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
    "/filesystem/report.bin?download=true"
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.bin").write_bytes(os.urandom(_SIZE))
    return ws


@pytest.fixture
def app(workspace: Path) -> FastAPI:
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process", cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none")
        ),
    )
    assert os_env is not None
    registry = SessionResourceRegistry()
    registry._primary_envs["conv_test"] = os_env
    return create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as c:
        yield c


class _ShrinkingFile:
    """A download descriptor whose file is truncated on disk after the first read.

    Models a writer rewriting the file while a download streams it: the first
    chunk is served normally, then the file is emptied, so the next read from
    the real descriptor hits EOF short of the announced size.
    """

    def __init__(self, inner: BinaryIO, path: Path) -> None:
        self._inner = inner
        self._path = path
        self.reads = 0
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        chunk = self._inner.read(n)
        self.reads += 1
        if self.reads == 1:
            os.truncate(self._path, 0)
        return chunk

    def close(self) -> None:
        self.closed = True
        self._inner.close()


@pytest.fixture
def shrinking_download(monkeypatch: pytest.MonkeyPatch) -> list[_ShrinkingFile]:
    """Make every ``open_download`` hand out a :class:`_ShrinkingFile`; collects them."""
    opened: list[_ShrinkingFile] = []
    original = CallerProcessFilesystem.open_download

    async def _open(self: CallerProcessFilesystem, path: str) -> tuple[BinaryIO, Path, int]:
        fobj, resolved, size = await original(self, path)
        wrapped = _ShrinkingFile(fobj, resolved)
        opened.append(wrapped)
        return wrapped, resolved, size  # type: ignore[return-value]

    monkeypatch.setattr(CallerProcessFilesystem, "open_download", _open)
    return opened


@pytest.mark.asyncio
async def test_download_streams_the_whole_file(client: httpx.AsyncClient, workspace: Path) -> None:
    """Baseline: an unchanged file downloads completely with a matching Content-Length."""
    resp = await client.get(_DOWNLOAD_URL)

    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(_SIZE)
    assert resp.headers["content-disposition"] == 'attachment; filename="report.bin"'
    assert resp.content == (workspace / "report.bin").read_bytes()


@pytest.mark.asyncio
async def test_download_aborts_and_logs_when_file_shrinks_mid_stream(
    client: httpx.AsyncClient,
    shrinking_download: list[_ShrinkingFile],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A short read ends the stream with an error, not a clean short body."""
    caplog.set_level(logging.WARNING, logger="omnigent.runner.app")

    with pytest.raises(RuntimeError, match=r"report\.bin shrank during download"):
        await client.get(_DOWNLOAD_URL)

    (fobj,) = shrinking_download
    assert fobj.closed, "the descriptor must be released when the stream aborts"
    record = next(r for r in caplog.records if "shrank while streaming" in r.getMessage())
    assert f"sent {_CHUNK} of {_SIZE} announced bytes" in record.getMessage()
    assert "report.bin" in record.getMessage()


@pytest.fixture
def served_app(app: FastAPI) -> Iterator[str]:
    """Run the runner app under real uvicorn on an ephemeral port; yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("runner app did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_download_over_the_wire_fails_instead_of_truncating(
    served_app: str,
    shrinking_download: list[_ShrinkingFile],
) -> None:
    """Through a real HTTP server the client sees a broken transfer, never a short 200 body."""
    with pytest.raises(httpx.RemoteProtocolError) as excinfo:
        httpx.get(f"{served_app}{_DOWNLOAD_URL}", timeout=10)

    # h11 names the shortfall against the announced Content-Length.
    assert f"received {_CHUNK} bytes, expected {_SIZE}" in str(excinfo.value)
