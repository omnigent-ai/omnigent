"""The workspace API (``omnigent.api.workspace.v1alpha1``), served by the host.

This is the API's single implementation: the desktop app's direct socket and
the server's route both reach this service. It reads through
:class:`~omnigent.workspace_fs.WorkspaceReader`, so its answers match the REST
file routes. Which workspace a call reads comes from the caller-supplied
``reader_for``, which resolves it from the call's credentials.

Mount it with the generated ASGI app::

    WorkspaceServiceASGIApplication(WorkspaceService(reader_for))
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import ParamSpec, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.request import RequestContext
from google.protobuf.timestamp_pb2 import Timestamp

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    CONTENT_ENCODING_BINARY,
    CONTENT_ENCODING_UTF8,
    ENTRY_TYPE_DIRECTORY,
    ENTRY_TYPE_FILE,
    Entry,
    FileMetadata,
    GetCapabilitiesRequest,
    GetCapabilitiesResponse,
    ListDirectoryRequest,
    ListDirectoryResponse,
    ReadFileRequest,
    ReadFileResponse,
)
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError

# Matches the largest page the web Files tree asks for.
_MAX_PAGE_SIZE = 1000
# Small enough to keep a stream responsive, large enough to keep framing cheap.
_READ_CHUNK_BYTES = 64 * 1024
_CAPABILITIES = ("ListDirectory", "ReadFile")
_ERROR_CODES = {400: Code.INVALID_ARGUMENT, 404: Code.NOT_FOUND}

ReaderForCall = Callable[[RequestContext], WorkspaceReader]
_Params = ParamSpec("_Params")
_T = TypeVar("_T")


async def _read(fn: Callable[_Params, _T], *args: _Params.args, **kwargs: _Params.kwargs) -> _T:
    """Run a blocking reader call off the event loop, mapping its errors."""
    try:
        return await run_sync_on_thread(fn, *args, **kwargs)
    except WorkspaceReaderError as exc:
        raise ConnectError(_ERROR_CODES.get(exc.status, Code.INTERNAL), exc.message) from exc


class WorkspaceService:
    """Implements ``WorkspaceService`` from ``workspace.proto``.

    :param reader_for: Returns the reader for the workspace a call may read.
        It must raise :class:`ConnectError` when the call isn't authorized.
    """

    def __init__(self, reader_for: ReaderForCall) -> None:
        self._reader_for = reader_for

    async def get_capabilities(
        self,
        _request: GetCapabilitiesRequest,
        _ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse],
    ) -> GetCapabilitiesResponse:
        return GetCapabilitiesResponse(capabilities=_CAPABILITIES)

    async def list_directory(
        self,
        request: ListDirectoryRequest,
        ctx: RequestContext[ListDirectoryRequest, ListDirectoryResponse],
    ) -> ListDirectoryResponse:
        if request.page_size < 0:
            raise ConnectError(Code.INVALID_ARGUMENT, "page_size must not be negative")
        reader = self._reader_for(ctx)
        page = await _read(
            reader.list_directory,
            request.path,
            limit=min(request.page_size or _MAX_PAGE_SIZE, _MAX_PAGE_SIZE),
            after=request.page_token or None,
        )
        entries = [
            Entry(
                name=item["name"],
                path=item["path"],
                type=ENTRY_TYPE_DIRECTORY if item["type"] == "directory" else ENTRY_TYPE_FILE,
                size=item["bytes"],
                modify_time=Timestamp(seconds=item["modified_at"]),
            )
            for item in page.data
        ]
        # The cursor is the last entry's id; the proto keeps it opaque.
        next_page_token = page.last_id if page.has_more and page.last_id else ""
        return ListDirectoryResponse(entries=entries, next_page_token=next_page_token)

    async def read_file(
        self,
        request: ReadFileRequest,
        ctx: RequestContext[ReadFileRequest, ReadFileResponse],
    ) -> AsyncIterator[ReadFileResponse]:
        if not request.path:
            raise ConnectError(Code.INVALID_ARGUMENT, "path is required")
        reader = self._reader_for(ctx)
        file = await _read(reader.read_file_bytes, request.path)
        yield ReadFileResponse(
            metadata=FileMetadata(
                size=len(file.data),
                truncated=file.truncated,
                encoding=CONTENT_ENCODING_UTF8 if file.is_text else CONTENT_ENCODING_BINARY,
                content_type=file.content_type or "",
            )
        )
        for start in range(0, len(file.data), _READ_CHUNK_BYTES):
            yield ReadFileResponse(data=file.data[start : start + _READ_CHUNK_BYTES])
