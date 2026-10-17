"""Session-scoped file routes keep the session and file ids inside the session path."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from omnigent_client._files import FilesNamespace


def _make_files(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[FilesNamespace, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srv")
    return FilesNamespace(client, "http://srv"), client


@pytest.mark.asyncio
async def test_file_routes_target_the_session_files_collection() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.raw_path.decode()}")
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"bytes")
        return httpx.Response(
            200, json={"id": "file_1", "name": "a.txt", "metadata": {"filename": "a.txt"}}
        )

    files, client = _make_files(handler)
    try:
        session_files = files.for_session("conv_abc")
        await session_files.get("file_1")
        await session_files.get_content("file_1")
        await session_files.delete("file a#1")
    finally:
        await client.aclose()

    assert calls == [
        "GET /v1/sessions/conv_abc/resources/files/file_1",
        "GET /v1/sessions/conv_abc/resources/files/file_1/content",
        "DELETE /v1/sessions/conv_abc/resources/files/file%20a%231",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda files: files.for_session("conv_abc/permissions/alice").list(),
            id="session-id-with-slash",
        ),
        pytest.param(
            lambda files: files.for_session("conv_abc").delete("file_1/../other"),
            id="file-id-with-slash",
        ),
        pytest.param(lambda files: files.for_session("conv_abc").get(""), id="empty-file-id"),
    ],
)
async def test_file_routes_reject_ids_that_escape_the_session_path(
    call: Callable[[FilesNamespace], Awaitable[object]],
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"data": []})

    files, client = _make_files(handler)
    try:
        with pytest.raises(ValueError):
            await call(files)
    finally:
        await client.aclose()

    assert calls == []
