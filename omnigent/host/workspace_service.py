"""The workspace API (``omnigent.api.workspace.v1alpha1``), served by the host.

This is the API's single implementation: the desktop app's direct socket and
the server's route both reach this service. It reads through
:class:`~omnigent.workspace_fs.WorkspaceReader`, so its answers match the REST
file routes. Which workspace a call reads comes from the caller-supplied
``reader_for``, which resolves it from the call's credentials.

Mount it with the generated ASGI app::

    WorkspaceServiceASGIApplication(WorkspaceService(reader_for))

:func:`start_workspace_socket` serves it to the session owner's desktop app on
an owner-only Unix socket, admitting calls that carry a server-issued token
(see :mod:`omnigent.host.workspace_tokens`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import ParamSpec, TypeVar

import uvicorn
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.request import RequestContext
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from omnigent.api.workspace.v1alpha1.workspace_connect import (
    WorkspaceService as WorkspaceServiceMethods,
)
from omnigent.api.workspace.v1alpha1.workspace_connect import WorkspaceServiceASGIApplication
from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    CONTENT_ENCODING_BINARY,
    CONTENT_ENCODING_UTF8,
    ENTRY_TYPE_DIRECTORY,
    ENTRY_TYPE_FILE,
    Entry,
    FileMetadata,
    GetCapabilitiesRequest,
    GetCapabilitiesResponse,
    ListBranchCommitsRequest,
    ListBranchCommitsResponse,
    ListDirectoryRequest,
    ListDirectoryResponse,
    ListRepositoryRefsRequest,
    ListRepositoryRefsResponse,
    ListWorkingCopyChangesRequest,
    ListWorkingCopyChangesResponse,
    ReadBaseFileRequest,
    ReadBaseFileResponse,
    ReadFileRequest,
    ReadFileResponse,
    ReadLinkTextRequest,
    ReadLinkTextResponse,
    ResolveRepositoryContextRequest,
    ResolveRepositoryContextResponse,
    ResolveWorkingCopyBaseRequest,
    ResolveWorkingCopyBaseResponse,
    SearchFilesRequest,
    SearchFilesResponse,
    SearchTextRequest,
    SearchTextResponse,
    StatPathRequest,
    StatPathResponse,
)
from omnigent.host.frames import HostWorkspaceCallFrame, HostWorkspaceReplyFrame
from omnigent.host.workspace_git import comparison, context, working_copy
from omnigent.host.workspace_methods import WORKSPACE_METHODS
from omnigent.host.workspace_search import search_files, search_text
from omnigent.host.workspace_tokens import new_token_key, verify_token
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.util.uvicorn_server import NoSignalServer
from omnigent.workspace_fs import ListedEntry, WorkspaceReader, WorkspaceReaderError

_logger = logging.getLogger(__name__)

# Matches the largest page the web Files tree asks for.
_MAX_PAGE_SIZE = 1000
# Small enough to keep a stream responsive, large enough to keep framing cheap.
_READ_CHUNK_BYTES = 64 * 1024
_ERROR_CODES = {
    400: Code.INVALID_ARGUMENT,
    404: Code.NOT_FOUND,
    412: Code.FAILED_PRECONDITION,
}
# A local socket bind; seconds means something is wrong, and the host goes on without it.
_SOCKET_STARTUP_TIMEOUT_S = 5.0
_SOCKET_SHUTDOWN_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class _Served:
    """How this host serves one method.

    :param features: Optional features it offers, e.g. ``"search_files.substring"``.
    :param tool: A program it needs on ``PATH``; without it, the method isn't listed.
    """

    features: tuple[str, ...] = ()
    tool: str | None = None


# The methods this host answers. Only these, and GetCapabilities, are dispatched.
_SERVED: dict[str, _Served] = {
    "ListDirectory": _Served(),
    "ReadFile": _Served(),
    "StatPath": _Served(),
    "ReadLinkText": _Served(),
    "SearchFiles": _Served(features=("search_files.substring",), tool="git"),
    "SearchText": _Served(tool="rg"),
    "ResolveWorkingCopyBase": _Served(features=("revision_comparison",), tool="git"),
    "ListWorkingCopyChanges": _Served(tool="git"),
    "ReadBaseFile": _Served(tool="git"),
    "ListRepositoryRefs": _Served(tool="git"),
    "ListBranchCommits": _Served(features=("branch_commits",), tool="git"),
    "ResolveRepositoryContext": _Served(tool="git"),
}


def capabilities() -> list[str]:
    """The method and feature names GetCapabilities lists on this host."""
    names: list[str] = []
    for name, served in _SERVED.items():
        if served.tool is None or shutil.which(served.tool):
            names += [name, *served.features]
    return names


# The context is ``None`` for calls that arrive over the host tunnel.
ReaderForCall = Callable[[RequestContext | None], WorkspaceReader]
_Params = ParamSpec("_Params")
_T = TypeVar("_T")


async def _read(fn: Callable[_Params, _T], *args: _Params.args, **kwargs: _Params.kwargs) -> _T:
    """Run a blocking reader call off the event loop, mapping its errors."""
    try:
        return await run_sync_on_thread(fn, *args, **kwargs)
    except WorkspaceReaderError as exc:
        raise ConnectError(_ERROR_CODES.get(exc.status, Code.INTERNAL), exc.message) from exc


class WorkspaceService(WorkspaceServiceMethods):
    """Implements ``WorkspaceService`` from ``workspace.proto``.

    Methods it doesn't define answer UNIMPLEMENTED, and GetCapabilities
    doesn't list them.

    :param reader_for: Returns the reader for the workspace a call may read.
        It must raise :class:`ConnectError` when the call isn't authorized.
    """

    def __init__(self, reader_for: ReaderForCall) -> None:
        self._reader_for = reader_for

    async def get_capabilities(
        self,
        _request: GetCapabilitiesRequest,
        _ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse] | None,
    ) -> GetCapabilitiesResponse:
        return GetCapabilitiesResponse(capabilities=capabilities())

    async def list_directory(
        self,
        request: ListDirectoryRequest,
        ctx: RequestContext[ListDirectoryRequest, ListDirectoryResponse] | None,
    ) -> ListDirectoryResponse:
        if request.page_size < 0:
            raise ConnectError(Code.INVALID_ARGUMENT, "page_size must not be negative")
        reader = await _read(self._reader_for, ctx)
        page = await _read(
            reader.list_directory,
            request.path,
            limit=min(request.page_size or _MAX_PAGE_SIZE, _MAX_PAGE_SIZE),
            after=request.page_token or None,
        )
        # The REST listing has no symlink flag, so look at each entry itself.
        links = await _read(_symlinks, reader.root, [item["path"] for item in page.data])
        entries = [_entry(item, is_symlink=item["path"] in links) for item in page.data]
        # The cursor is the last entry's id; the proto keeps it opaque.
        next_page_token = page.last_id if page.has_more and page.last_id else ""
        return ListDirectoryResponse(entries=entries, next_page_token=next_page_token)

    async def read_file(
        self,
        request: ReadFileRequest,
        ctx: RequestContext[ReadFileRequest, ReadFileResponse] | None,
    ) -> AsyncIterator[ReadFileResponse]:
        if not request.path:
            raise ConnectError(Code.INVALID_ARGUMENT, "path is required")
        reader = await _read(self._reader_for, ctx)
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

    async def stat_path(
        self,
        request: StatPathRequest,
        ctx: RequestContext[StatPathRequest, StatPathResponse] | None,
    ) -> StatPathResponse:
        reader = await _read(self._reader_for, ctx)
        item, is_symlink = await _read(reader.stat_entry, request.path)
        return StatPathResponse(entry=_entry(item, is_symlink=is_symlink))

    async def resolve_working_copy_base(
        self,
        request: ResolveWorkingCopyBaseRequest,
        ctx: RequestContext[ResolveWorkingCopyBaseRequest, ResolveWorkingCopyBaseResponse] | None,
    ) -> ResolveWorkingCopyBaseResponse:
        reader = await _read(self._reader_for, ctx)
        return await working_copy.resolve_working_copy_base(reader.root, request)

    async def list_working_copy_changes(
        self,
        request: ListWorkingCopyChangesRequest,
        ctx: RequestContext[ListWorkingCopyChangesRequest, ListWorkingCopyChangesResponse] | None,
    ) -> ListWorkingCopyChangesResponse:
        reader = await _read(self._reader_for, ctx)
        return await working_copy.list_working_copy_changes(reader.root, request)

    async def read_base_file(
        self,
        request: ReadBaseFileRequest,
        ctx: RequestContext[ReadBaseFileRequest, ReadBaseFileResponse] | None,
    ) -> AsyncIterator[ReadBaseFileResponse]:
        reader = await _read(self._reader_for, ctx)
        async with aclosing(working_copy.read_base_file(reader.root, request)) as responses:
            async for response in responses:
                yield response

    async def list_repository_refs(
        self,
        request: ListRepositoryRefsRequest,
        ctx: RequestContext[ListRepositoryRefsRequest, ListRepositoryRefsResponse] | None,
    ) -> ListRepositoryRefsResponse:
        reader = await _read(self._reader_for, ctx)
        return await comparison.list_repository_refs(reader.root, request)

    async def list_branch_commits(
        self,
        request: ListBranchCommitsRequest,
        ctx: RequestContext[ListBranchCommitsRequest, ListBranchCommitsResponse] | None,
    ) -> ListBranchCommitsResponse:
        reader = await _read(self._reader_for, ctx)
        return await comparison.list_branch_commits(reader.root, request)

    async def resolve_repository_context(
        self,
        request: ResolveRepositoryContextRequest,
        ctx: RequestContext[ResolveRepositoryContextRequest, ResolveRepositoryContextResponse]
        | None,
    ) -> ResolveRepositoryContextResponse:
        reader = await _read(self._reader_for, ctx)
        return await context.resolve_repository_context(reader.root, request)

    async def search_files(
        self,
        request: SearchFilesRequest,
        ctx: RequestContext[SearchFilesRequest, SearchFilesResponse] | None,
    ) -> SearchFilesResponse:
        reader = await _read(self._reader_for, ctx)
        return await search_files(reader.root, request)

    async def search_text(
        self,
        request: SearchTextRequest,
        ctx: RequestContext[SearchTextRequest, SearchTextResponse] | None,
    ) -> AsyncIterator[SearchTextResponse]:
        reader = await _read(self._reader_for, ctx)
        # Closing this stream early must stop ripgrep, not leave it to the GC.
        async with aclosing(search_text(reader.root, request)) as responses:
            async for response in responses:
                yield response

    async def read_link_text(
        self,
        request: ReadLinkTextRequest,
        ctx: RequestContext[ReadLinkTextRequest, ReadLinkTextResponse] | None,
    ) -> ReadLinkTextResponse:
        if not request.path:
            raise ConnectError(Code.INVALID_ARGUMENT, "path is required")
        reader = await _read(self._reader_for, ctx)
        # The link may point anywhere; only its parent must be in the workspace.
        return ReadLinkTextResponse(target=await _read(reader.read_link_text, request.path))


def _entry(item: ListedEntry, *, is_symlink: bool) -> Entry:
    return Entry(
        name=item["name"],
        path=item["path"],
        type=ENTRY_TYPE_DIRECTORY if item["type"] == "directory" else ENTRY_TYPE_FILE,
        size=item["bytes"],
        modify_time=Timestamp(seconds=item["modified_at"]),
        is_symlink=is_symlink,
    )


def _symlinks(root: Path, paths: list[str]) -> set[str]:
    """The ``paths`` under ``root`` that are symlinks themselves."""
    return {path for path in paths if (root / path).is_symlink()}


def _reader_at(
    reader_for_workspace: Callable[[str], WorkspaceReader], workspace: str
) -> WorkspaceReader:
    """Return the reader for a recorded workspace that exists on this host."""
    expanded = os.path.expanduser(workspace)
    if not os.path.isdir(expanded):
        raise ConnectError(Code.NOT_FOUND, "The session's workspace is not on this host")
    return reader_for_workspace(expanded)


async def _call(service: WorkspaceService, method: str, request: bytes) -> AsyncIterator[Message]:
    """Run one serialized call against ``service``, yielding its responses.

    The handler comes from the method table, never from an attribute the
    caller names, so only served methods can run.
    """
    if method != "GetCapabilities" and method not in _SERVED:
        raise ConnectError(Code.UNIMPLEMENTED, f"Unknown workspace method {method!r}")
    served = WORKSPACE_METHODS[method]
    handler = getattr(service, served.python_name)
    parsed = served.request_type.FromString(request)
    if not served.streaming:
        yield await handler(parsed, None)
        return
    # Closing this call early must stop the stream's work, not leave it to the GC.
    async with aclosing(handler(parsed, None)) as responses:
        async for response in responses:
            yield response


async def serve_tunnel_call(
    reader_for_workspace: Callable[[str], WorkspaceReader], frame: HostWorkspaceCallFrame
) -> AsyncIterator[HostWorkspaceReplyFrame]:
    """Answer a workspace call the server forwarded over the host tunnel.

    The server has already authorized the caller and chosen the workspace.
    Yields one reply per response message, then a final ``done`` reply that
    carries the error, if any.
    """
    service = WorkspaceService(lambda _ctx: _reader_at(reader_for_workspace, frame.workspace))
    request_id = frame.request_id
    try:
        async for response in _call(service, frame.method, frame.request):
            yield HostWorkspaceReplyFrame(request_id, message=response.SerializeToString())
    except ConnectError as exc:
        yield HostWorkspaceReplyFrame(
            request_id, done=True, error_code=exc.code.value, error_message=exc.message
        )
        return
    except Exception:
        # The server would otherwise wait out its timeout for a reply that never comes.
        _logger.exception("Workspace call %s failed", frame.method)
        yield HostWorkspaceReplyFrame(
            request_id, done=True, error_code="internal", error_message="Workspace call failed"
        )
        return
    yield HostWorkspaceReplyFrame(request_id, done=True)


class WorkspaceSocket:
    """The workspace API on an owner-only Unix socket.

    :param path: The socket, inside a directory only this user can enter.
    :param token_key: Key the server signs this socket's tokens with.
    """

    def __init__(
        self, server: uvicorn.Server, task: asyncio.Task[None], path: Path, token_key: str
    ) -> None:
        self._server = server
        self._task = task
        self.path = path
        self.token_key = token_key

    async def stop(self) -> None:
        """Stop serving and remove the socket's directory (best-effort)."""
        self._server.should_exit = True
        await asyncio.wait({self._task}, timeout=_SOCKET_SHUTDOWN_TIMEOUT_S)
        if not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=_SOCKET_SHUTDOWN_TIMEOUT_S)
        shutil.rmtree(self.path.parent, ignore_errors=True)


async def start_workspace_socket(
    reader_for_workspace: Callable[[str], WorkspaceReader], *, host_id: str
) -> WorkspaceSocket | None:
    """Serve the workspace API on a fresh owner-only Unix socket.

    A call must carry ``Authorization: Bearer <token>`` with a token minted for
    this host; the token names the workspace it reads.

    :param reader_for_workspace: Returns the reader for an existing workspace
        directory, e.g. the host's shared reader cache.
    :param host_id: This host's id, which tokens must name.
    :returns: The running socket, or ``None`` when it could not start. Direct
        access is an optimization, so the host carries on without it.
    """
    token_key = new_token_key()

    def reader_for(ctx: RequestContext | None) -> WorkspaceReader:
        authorization = (ctx.request_headers.get("authorization") if ctx else None) or ""
        scheme, _, token = authorization.partition(" ")
        grant = (
            verify_token(token_key, token, host_id=host_id)
            if scheme.lower() == "bearer" and token
            else None
        )
        if grant is None:
            raise ConnectError(Code.UNAUTHENTICATED, "A valid workspace token is required")
        return _reader_at(reader_for_workspace, grant.workspace)

    # Bind here rather than in uvicorn, whose bind failure calls sys.exit().
    directory: Path | None = None
    try:
        # mkdtemp's directory is mode 0700, so no other user can reach the socket.
        directory = Path(tempfile.mkdtemp(prefix="omnigent-workspace-"))
        path = directory / "workspace.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            path.chmod(0o600)
        except OSError:
            sock.close()
            raise
    except (OSError, AttributeError):  # AttributeError: no AF_UNIX on this platform
        _logger.warning(
            "Workspace socket could not be created; direct access is off", exc_info=True
        )
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        return None
    app = WorkspaceServiceASGIApplication(WorkspaceService(reader_for))
    config = uvicorn.Config(
        app, ws="none", lifespan="off", log_level="warning", access_log=False, log_config=None
    )
    # The host owns SIGINT/SIGTERM; a plain uvicorn.Server would take them over.
    server = NoSignalServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="host-workspace-socket")
    listener = WorkspaceSocket(server, task, path, token_key)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SOCKET_STARTUP_TIMEOUT_S
    while not server.started:
        if task.done() or loop.time() >= deadline:
            failure = task.exception() if task.done() and not task.cancelled() else None
            _logger.warning(
                "Workspace socket failed to start; direct access is off", exc_info=failure
            )
            await listener.stop()
            return None
        await asyncio.sleep(0.01)
    return listener
