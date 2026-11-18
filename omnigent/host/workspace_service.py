"""The workspace API (``omnigent.api.workspace.v1alpha1``), served by the host.

This is the API's single implementation: the desktop app's direct socket and
the server's route both reach this service. It forwards to the host's
workspace helper (:mod:`omnigent.host.workspace_helper`) the methods the
helper serves, and reads ListDirectory and ReadFile through
:class:`~omnigent.workspace_fs.WorkspaceReader` when there is no helper, so
its answers match the REST file routes either way. Which workspace a call
reads comes from the caller-supplied ``reader_for``, which resolves it from
the call's credentials; the helper only ever sees that workspace.

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
from pathlib import Path
from typing import ParamSpec, TypeVar, cast

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
    ListDirectoryRequest,
    ListDirectoryResponse,
    ReadFileRequest,
    ReadFileResponse,
)
from omnigent.host.frames import HostWorkspaceCallFrame, HostWorkspaceReplyFrame
from omnigent.host.workspace_helper.process import HelperUnavailable
from omnigent.host.workspace_helper.proxy import WorkspaceHelperProxy
from omnigent.host.workspace_methods import (
    BASELINE_METHODS,
    CLIENT_FEATURES,
    HELPER_METHODS,
    WORKSPACE_METHODS,
    WorkspaceMethod,
)
from omnigent.host.workspace_tokens import new_token_key, verify_token
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.util.uvicorn_server import NoSignalServer
from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError, guess_content_type

_logger = logging.getLogger(__name__)

# Matches the largest page the web Files tree asks for.
_MAX_PAGE_SIZE = 1000
# Small enough to keep a stream responsive, large enough to keep framing cheap.
_READ_CHUNK_BYTES = 64 * 1024
_CAPABILITIES = ("ListDirectory", "ReadFile")
_ERROR_CODES = {400: Code.INVALID_ARGUMENT, 404: Code.NOT_FOUND}
# A local socket bind; seconds means something is wrong, and the host goes on without it.
_SOCKET_STARTUP_TIMEOUT_S = 5.0
_SOCKET_SHUTDOWN_TIMEOUT_S = 2.0

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
    :param helper: The host's workspace helper, or ``None`` to serve only
        ListDirectory and ReadFile, from the reader.
    """

    def __init__(
        self, reader_for: ReaderForCall, helper: WorkspaceHelperProxy | None = None
    ) -> None:
        self._reader_for = reader_for
        self._helper = helper

    async def get_capabilities(
        self,
        _request: GetCapabilitiesRequest,
        ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse] | None,
    ) -> GetCapabilitiesResponse:
        names = list(_CAPABILITIES)
        if self._helper is not None and self._helper.enabled:
            # Only a caller that may read the workspace gets a helper started.
            await _read(self._reader_for, ctx)
            served = await self._helper.capabilities()
            names += sorted((served & (HELPER_METHODS | CLIENT_FEATURES)) - set(names))
        return GetCapabilitiesResponse(capabilities=names)

    async def list_directory(
        self,
        request: ListDirectoryRequest,
        ctx: RequestContext[ListDirectoryRequest, ListDirectoryResponse] | None,
    ) -> ListDirectoryResponse:
        if request.page_size < 0:
            raise ConnectError(Code.INVALID_ARGUMENT, "page_size must not be negative")
        reader = await _read(self._reader_for, ctx)
        method = WORKSPACE_METHODS["ListDirectory"]
        if await self._helper_serves(method):
            try:
                response = await self._forward_unary(method, reader, request)
            except _HelperGone as exc:
                _log_fallback(method, exc)
            else:
                return cast("ListDirectoryResponse", response)
        page = await _read(
            reader.list_directory,
            request.path,
            limit=min(request.page_size or _MAX_PAGE_SIZE, _MAX_PAGE_SIZE),
            after=request.page_token or None,
        )
        # The REST listing has no symlink flag, so look at each entry itself.
        links = await _read(_symlinks, reader.root, [item["path"] for item in page.data])
        entries = [
            Entry(
                name=item["name"],
                path=item["path"],
                type=ENTRY_TYPE_DIRECTORY if item["type"] == "directory" else ENTRY_TYPE_FILE,
                size=item["bytes"],
                modify_time=Timestamp(seconds=item["modified_at"]),
                is_symlink=item["path"] in links,
            )
            for item in page.data
        ]
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
        method = WORKSPACE_METHODS["ReadFile"]
        if await self._helper_serves(method):
            sent = False
            try:
                async for response in self._forward_stream(method, reader, request):
                    sent = True
                    message = cast("ReadFileResponse", response)
                    if message.HasField("metadata"):
                        # The helper leaves the type to the host, so it matches REST here.
                        message.metadata.content_type = guess_content_type(request.path) or ""
                    yield message
                return
            except _HelperGone as exc:
                # A read the helper started can't be finished by another reader.
                if sent:
                    raise ConnectError(
                        Code.UNAVAILABLE, "The workspace helper stopped during the read"
                    ) from None
                _log_fallback(method, exc)
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

    async def _helper_serves(self, method: WorkspaceMethod) -> bool:
        if self._helper is None or not self._helper.enabled:
            return False
        return method.name in await self._helper.capabilities()

    async def _forward_unary(
        self, method: WorkspaceMethod, reader: WorkspaceReader, request: Message
    ) -> Message:
        """Answer one call from the helper, in the caller's workspace."""
        assert self._helper is not None
        try:
            async with self._helper.call(reader.root) as call:
                return await getattr(call.client, method.python_name)(
                    request, headers=call.headers
                )
        except HelperUnavailable as exc:
            raise _HelperGone(str(exc)) from exc
        except ConnectError as exc:
            if exc.code == Code.UNAVAILABLE:
                raise _HelperGone(exc.message) from exc
            raise

    async def _forward_stream(
        self, method: WorkspaceMethod, reader: WorkspaceReader, request: Message
    ) -> AsyncIterator[Message]:
        """Stream one call's answers from the helper, leasing the workspace throughout."""
        assert self._helper is not None
        try:
            async with self._helper.call(reader.root) as call:
                responses = getattr(call.client, method.python_name)(request, headers=call.headers)
                # Closing early resets the helper's stream instead of leaving it to the GC.
                async with aclosing(responses) as stream:
                    async for response in stream:
                        yield response
        except HelperUnavailable as exc:
            raise _HelperGone(str(exc)) from exc
        except ConnectError as exc:
            if exc.code == Code.UNAVAILABLE:
                raise _HelperGone(exc.message) from exc
            raise


def _symlinks(root: Path, paths: list[str]) -> set[str]:
    """The ``paths`` under ``root`` that are symlinks themselves."""
    return {path for path in paths if (root / path).is_symlink()}


def _log_fallback(method: WorkspaceMethod, exc: ConnectError) -> None:
    _logger.warning(
        "The workspace helper couldn't answer %s, so the host's reader did: %s",
        method.name,
        exc.message,
    )


class _HelperGone(ConnectError):
    """No helper can answer now: it isn't running, or stopped during the call."""

    def __init__(self, message: str) -> None:
        super().__init__(Code.UNAVAILABLE, message)


def _helper_forwarder(method: WorkspaceMethod) -> Callable[..., object]:
    """A WorkspaceService method that only the helper serves."""
    not_served = f"This host doesn't serve {method.name}"

    if method.streaming:

        async def forward_stream(
            self: WorkspaceService, request: Message, ctx: RequestContext | None
        ) -> AsyncIterator[Message]:
            reader = await _read(self._reader_for, ctx)
            if not await self._helper_serves(method):
                raise ConnectError(Code.UNIMPLEMENTED, not_served)
            async for response in self._forward_stream(method, reader, request):
                yield response

        forward_stream.__name__ = method.python_name
        return forward_stream

    async def forward(
        self: WorkspaceService, request: Message, ctx: RequestContext | None
    ) -> Message:
        reader = await _read(self._reader_for, ctx)
        if not await self._helper_serves(method):
            raise ConnectError(Code.UNIMPLEMENTED, not_served)
        return await self._forward_unary(method, reader, request)

    forward.__name__ = method.python_name
    return forward


for _method in WORKSPACE_METHODS.values():
    if _method.name in HELPER_METHODS - BASELINE_METHODS:
        setattr(WorkspaceService, _method.python_name, _helper_forwarder(_method))


def _reader_at(
    reader_for_workspace: Callable[[str], WorkspaceReader], workspace: str
) -> WorkspaceReader:
    """Return the reader for a recorded workspace that exists on this host."""
    expanded = os.path.expanduser(workspace)
    if not os.path.isdir(expanded):
        raise ConnectError(Code.NOT_FOUND, "The session's workspace is not on this host")
    return reader_for_workspace(expanded)


async def _call(service: WorkspaceService, name: str, request: bytes) -> AsyncIterator[Message]:
    """Run one serialized call against ``service``, yielding its responses."""
    method = WORKSPACE_METHODS.get(name)
    if method is None:
        raise ConnectError(Code.UNIMPLEMENTED, f"Unknown workspace method {name!r}")
    handler = getattr(service, method.python_name)
    message = method.request_type.FromString(request)
    if method.streaming:
        async with aclosing(handler(message, None)) as responses:
            async for response in responses:
                yield response
    else:
        yield await handler(message, None)


async def serve_tunnel_call(
    reader_for_workspace: Callable[[str], WorkspaceReader],
    frame: HostWorkspaceCallFrame,
    helper: WorkspaceHelperProxy | None = None,
) -> AsyncIterator[HostWorkspaceReplyFrame]:
    """Answer a workspace call the server forwarded over the host tunnel.

    The server has already authorized the caller and chosen the workspace.
    Yields one reply per response message, then a final ``done`` reply that
    carries the error, if any.

    :param helper: The host's workspace helper, shared with its socket.
    """
    service = WorkspaceService(
        lambda _ctx: _reader_at(reader_for_workspace, frame.workspace), helper
    )
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
    reader_for_workspace: Callable[[str], WorkspaceReader],
    *,
    host_id: str,
    helper: WorkspaceHelperProxy | None = None,
) -> WorkspaceSocket | None:
    """Serve the workspace API on a fresh owner-only Unix socket.

    A call must carry ``Authorization: Bearer <token>`` with a token minted for
    this host; the token names the workspace it reads.

    :param reader_for_workspace: Returns the reader for an existing workspace
        directory, e.g. the host's shared reader cache.
    :param host_id: This host's id, which tokens must name.
    :param helper: The host's workspace helper, shared with its tunnel.
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
    app = WorkspaceServiceASGIApplication(WorkspaceService(reader_for, helper))
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
