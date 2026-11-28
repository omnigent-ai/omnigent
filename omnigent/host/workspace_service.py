"""The workspace API (``omnigent.api.workspace.v1alpha1``), served by the host.

This is the API's single implementation: the desktop app's direct socket and
the server's route both reach this service. It reads through
:class:`~omnigent.workspace_fs.WorkspaceReader`, so its answers match the REST
file routes. Which workspace a call reads comes from the caller-supplied
``reader_for``, which resolves it from the call's credentials. Its pull request
methods are served only over the tunnel, for the session the server sends with
the call (see :mod:`omnigent.host.workspace_pull_requests`); the socket serves none.

Mount it with the generated ASGI app::

    WorkspaceServiceASGIApplication(WorkspaceService(reader_for))

:func:`start_workspace_socket` serves it to the session owner's desktop app on
an owner-only Unix socket, admitting calls that carry a server-issued token
(see :mod:`omnigent.host.workspace_tokens`).
"""

from __future__ import annotations

import asyncio
import itertools
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
    PullRequestImageMetadata,
    ReadBaseFileRequest,
    ReadBaseFileResponse,
    ReadFileRequest,
    ReadFileResponse,
    ReadLinkTextRequest,
    ReadLinkTextResponse,
    ReadPullRequestImageRequest,
    ReadPullRequestImageResponse,
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
from omnigent.entities.pagination import PagedList
from omnigent.host import workspace_fuzzy
from omnigent.host.frames import HostWorkspaceCallFrame, HostWorkspaceReplyFrame
from omnigent.host.workspace_git import comparison, context, working_copy
from omnigent.host.workspace_git.ignored import DirectoryIgnores
from omnigent.host.workspace_git.repository_folder import repository_folder
from omnigent.host.workspace_git.runner import Deadline, GitError, runs_on_this_host
from omnigent.host.workspace_methods import WORKSPACE_METHODS
from omnigent.host.workspace_payloads import payload_struct
from omnigent.host.workspace_pull_request_input import refuse_unserved_request
from omnigent.host.workspace_pull_requests import (
    PULL_REQUEST_METHODS,
    PULL_REQUEST_OPS,
    PullRequestCall,
    PullRequestRefused,
    run_pull_request_op,
    run_pull_request_stream_op,
)
from omnigent.host.workspace_search import search_files, search_text
from omnigent.host.workspace_tokens import new_token_key, verify_token
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.util.uvicorn_server import NoSignalServer
from omnigent.workspace_fs import ListedEntry, WorkspaceReader, WorkspaceReaderError

_logger = logging.getLogger(__name__)

# Matches the largest page the web Files tree asks for.
_MAX_PAGE_SIZE = 1000
# How long ListDirectory may ask Git which entries it ignores, across its pages.
_IGNORED_DEADLINE_S = 10.0
# Small enough to keep a stream responsive, large enough to keep framing cheap.
_READ_CHUNK_BYTES = 64 * 1024
# Image parts stay small, so reads in flight don't crowd the host tunnel's other calls.
_IMAGE_CHUNK_BYTES = 256 * 1024
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
    :param optional_features: Returns the features this host can offer only when
        what they need is there, such as a library that loads or a program on ``PATH``.
    """

    features: tuple[str, ...] = ()
    tool: str | None = None
    optional_features: Callable[[], tuple[str, ...]] | None = None


# The methods this host answers. Only these, and GetCapabilities, are dispatched.
_SERVED: dict[str, _Served] = {
    "ListDirectory": _Served(
        features=("list_directory.collapse_single_child_directories",),
        optional_features=lambda: ("gitignore",) if _reads_ignore_rules() else (),
    ),
    "ReadFile": _Served(),
    "StatPath": _Served(),
    "ReadLinkText": _Served(),
    "SearchFiles": _Served(
        features=("search_files.substring",),
        tool="git",
        optional_features=workspace_fuzzy.features,
    ),
    "SearchText": _Served(features=("text_search_priority",), tool="rg"),
    "ResolveWorkingCopyBase": _Served(
        features=("revision_comparison", "comparison.repository"), tool="git"
    ),
    "ListWorkingCopyChanges": _Served(tool="git"),
    "ReadBaseFile": _Served(tool="git"),
    "ListRepositoryRefs": _Served(tool="git"),
    "ListBranchCommits": _Served(features=("branch_commits", "branch_commits.head"), tool="git"),
    "ResolveRepositoryContext": _Served(tool="git"),
    # Served only over the tunnel, for a call the server sent with its session.
    **{name: _Served(tool="gh") for name in PULL_REQUEST_METHODS},
}


def _reads_ignore_rules() -> bool:
    """Whether listings can ask Git what it ignores: ``git`` is on ``PATH`` and can run here."""
    return bool(shutil.which("git")) and runs_on_this_host()


def capabilities(*, pull_requests: bool = False) -> list[str]:
    """The method and feature names GetCapabilities lists on this host.

    :param pull_requests: Whether the call may read pull requests: only a tunnel call
        that carries the session the server authorized.
    """
    names: list[str] = []
    for name, served in _SERVED.items():
        if name in PULL_REQUEST_METHODS and not pull_requests:
            continue
        if served.tool is None or shutil.which(served.tool):
            optional = served.optional_features() if served.optional_features else ()
            names += [name, *served.features, *optional]
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
    :param pull_request_call: The session and vouch the server sent with a tunnel call,
        or ``None``, e.g. on the direct socket, where pull requests aren't served.
    """

    def __init__(
        self, reader_for: ReaderForCall, pull_request_call: PullRequestCall | None = None
    ) -> None:
        self._reader_for = reader_for
        self._pull_request_call = pull_request_call

    async def get_capabilities(
        self,
        _request: GetCapabilitiesRequest,
        _ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse] | None,
    ) -> GetCapabilitiesResponse:
        return GetCapabilitiesResponse(
            capabilities=capabilities(pull_requests=self._pull_request_call is not None)
        )

    async def _serve_pull_request(
        self, name: str, request: Message, ctx: RequestContext | None
    ) -> Message:
        call = self._served_pull_request_call(name, request)
        reader = await _read(self._reader_for, ctx)
        payload = await _read(run_pull_request_op, name, reader, call, request)
        return WORKSPACE_METHODS[name].response_type(payload=payload_struct(payload))

    def _served_pull_request_call(self, name: str, request: Message) -> PullRequestCall:
        # Refused before any reader, registry or gh work when the server sent no session.
        call = self._pull_request_call
        if call is None:
            raise ConnectError(
                Code.UNIMPLEMENTED, "Pull requests are served through the server's route"
            )
        refuse_unserved_request(name, request)
        return call

    async def read_pull_request_image(
        self,
        request: ReadPullRequestImageRequest,
        ctx: RequestContext[ReadPullRequestImageRequest, ReadPullRequestImageResponse] | None,
    ) -> AsyncIterator[ReadPullRequestImageResponse]:
        name = "ReadPullRequestImage"
        call = self._served_pull_request_call(name, request)
        reader = await _read(self._reader_for, ctx)
        image = await _read(run_pull_request_stream_op, name, reader, call, request)
        yield ReadPullRequestImageResponse(
            metadata=PullRequestImageMetadata(
                content_type=image.content_type, size=len(image.data)
            )
        )
        for start in range(0, len(image.data), _IMAGE_CHUNK_BYTES):
            yield ReadPullRequestImageResponse(data=image.data[start : start + _IMAGE_CHUNK_BYTES])

    async def list_directory(
        self,
        request: ListDirectoryRequest,
        ctx: RequestContext[ListDirectoryRequest, ListDirectoryResponse] | None,
    ) -> ListDirectoryResponse:
        if request.page_size < 0:
            raise ConnectError(Code.INVALID_ARGUMENT, "page_size must not be negative")
        reader = await _read(self._reader_for, ctx)
        limit = min(request.page_size or _MAX_PAGE_SIZE, _MAX_PAGE_SIZE)
        deadline = Deadline(_IGNORED_DEADLINE_S)
        entries: list[Entry] = []
        # The cursor is the last consumed entry's id; the proto keeps it opaque.
        after = request.page_token or None

        async def read_page(after: str | None) -> PagedList[ListedEntry]:
            return await _read(
                reader.list_directory,
                request.path,
                limit=limit,
                after=after,
                collapse_single_child_directories=request.collapse_single_child_directories,
            )

        page = await read_page(after)
        # Git reads the confined directory the listing read; one repository answers for it all.
        directory = await _read(reader.resolve_directory, request.path)
        ignores = await _directory_ignores(directory, deadline)
        while True:
            paths = [item["path"] for item in page.data]
            # The REST listing has no symlink flag, so look at each entry itself.
            links = await _read(_symlinks, reader.root, paths)
            ignored = await _ignored(ignores, [item["name"] for item in page.data], deadline)
            if ignored is None and entries:
                # Git stopped answering partway: end here rather than mix in unfiltered pages.
                return ListDirectoryResponse(entries=entries, next_page_token=after or "")
            ignored = ignored or set()
            leaves = await _leaves_short_of_ignored(ignores, page.data, ignored, deadline)
            for index, item in enumerate(page.data):
                after = item["id"]
                hidden = request.hide_ignored and item["name"] in ignored
                if not hidden:
                    entries.append(
                        _entry(
                            {**item, "leaf_directory_path": leaves[item["path"]]}
                            if item["path"] in leaves
                            else item,
                            is_symlink=item["path"] in links,
                            ignored=item["name"] in ignored,
                        )
                    )
                if len(entries) == limit:
                    more = page.has_more or index < len(page.data) - 1
                    return ListDirectoryResponse(
                        entries=entries, next_page_token=after if more else ""
                    )
            # Hidden entries leave room, so read on: a page comes back empty only at the end.
            if not (request.hide_ignored and page.has_more and page.last_id):
                return ListDirectoryResponse(
                    entries=entries,
                    next_page_token=page.last_id if page.has_more and page.last_id else "",
                )
            after = page.last_id
            page = await read_page(after)

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
        folder = await repository_folder(reader, request.repository)
        return await working_copy.resolve_working_copy_base(
            folder.path, request, folder.repository
        )

    async def list_working_copy_changes(
        self,
        request: ListWorkingCopyChangesRequest,
        ctx: RequestContext[ListWorkingCopyChangesRequest, ListWorkingCopyChangesResponse] | None,
    ) -> ListWorkingCopyChangesResponse:
        reader = await _read(self._reader_for, ctx)
        folder = await repository_folder(reader, request.repository)
        return await working_copy.list_working_copy_changes(
            folder.path, request, folder.repository
        )

    async def read_base_file(
        self,
        request: ReadBaseFileRequest,
        ctx: RequestContext[ReadBaseFileRequest, ReadBaseFileResponse] | None,
    ) -> AsyncIterator[ReadBaseFileResponse]:
        reader = await _read(self._reader_for, ctx)
        folder = await repository_folder(reader, request.repository)
        async with aclosing(
            working_copy.read_base_file(folder.path, request, folder.repository)
        ) as responses:
            async for response in responses:
                yield response

    async def list_repository_refs(
        self,
        request: ListRepositoryRefsRequest,
        ctx: RequestContext[ListRepositoryRefsRequest, ListRepositoryRefsResponse] | None,
    ) -> ListRepositoryRefsResponse:
        reader = await _read(self._reader_for, ctx)
        folder = await repository_folder(reader, request.repository)
        return await comparison.list_repository_refs(folder.path, request, folder.repository)

    async def list_branch_commits(
        self,
        request: ListBranchCommitsRequest,
        ctx: RequestContext[ListBranchCommitsRequest, ListBranchCommitsResponse] | None,
    ) -> ListBranchCommitsResponse:
        reader = await _read(self._reader_for, ctx)
        folder = await repository_folder(reader, request.repository)
        return await comparison.list_branch_commits(folder.path, request, folder.repository)

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


async def _directory_ignores(directory: Path, deadline: Deadline) -> DirectoryIgnores | None:
    """The listed directory's ignore rules; none without Git, so the listing still answers."""
    if not _reads_ignore_rules():
        return None
    try:
        return await DirectoryIgnores.for_directory(directory, deadline)
    except GitError as exc:
        _logger.info("Listing without ignored files: %s", exc.problem.name)
        return None


async def _ignored(
    ignores: DirectoryIgnores | None, names: list[str], deadline: Deadline
) -> set[str] | None:
    """The ``names`` Git ignores, empty without a repository, or ``None`` when Git failed."""
    if ignores is None:
        return set()
    try:
        return await ignores.ignored(names, deadline)
    except GitError as exc:
        _logger.info("Listing without ignored files: %s", exc.problem.name)
        return None


async def _leaves_short_of_ignored(
    ignores: DirectoryIgnores | None,
    items: list[ListedEntry],
    ignored: set[str],
    deadline: Deadline,
) -> dict[str, str]:
    """Folder chains cut before the first folder Git ignores, so a hint never shows one.

    :returns: Each changed entry's path and its new ``leaf_directory_path``, ``""`` for none.
    """
    if ignores is None:
        return {}
    leaves: dict[str, str] = {}
    # Each chain's inner folders, as paths below the listed directory.
    inner: dict[str, list[str]] = {}
    for item in items:
        leaf = item.get("leaf_directory_path")
        if not leaf:
            continue
        if item["name"] in ignored:
            # Everything below an ignored folder is ignored too; it lists folder by folder.
            leaves[item["path"]] = ""
            continue
        below = leaf.removeprefix(item["path"] + "/").split("/")
        inner[item["path"]] = [
            "/".join([item["name"], *below[: depth + 1]]) for depth in range(len(below))
        ]
    if not inner:
        return leaves
    hit = await _ignored(ignores, [path for paths in inner.values() for path in paths], deadline)
    for path, folders in inner.items():
        if hit is None:
            # Git stopped answering: leave out the hint rather than risk an ignored folder.
            leaves[path] = ""
            continue
        kept = len(list(itertools.takewhile(lambda folder: folder not in hit, folders)))
        if kept < len(folders):
            leaves[path] = "/".join([path, *folders[kept - 1].split("/")[1:]]) if kept else ""
    return leaves


def _entry(item: ListedEntry, *, is_symlink: bool, ignored: bool = False) -> Entry:
    return Entry(
        name=item["name"],
        path=item["path"],
        type=ENTRY_TYPE_DIRECTORY if item["type"] == "directory" else ENTRY_TYPE_FILE,
        size=item["bytes"],
        modify_time=Timestamp(seconds=item["modified_at"]),
        is_symlink=is_symlink,
        leaf_directory_path=item.get("leaf_directory_path", ""),
        ignored=ignored,
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


def _pull_request_handler(name: str) -> Callable[..., object]:
    """A WorkspaceService method that serves the pull request method ``name``."""

    async def handle(
        self: WorkspaceService, request: Message, ctx: RequestContext | None
    ) -> Message:
        return await self._serve_pull_request(name, request, ctx)

    handle.__name__ = WORKSPACE_METHODS[name].python_name
    return handle


for _name in PULL_REQUEST_OPS:
    setattr(WorkspaceService, WORKSPACE_METHODS[_name].python_name, _pull_request_handler(_name))


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
    pull_request_call = (
        PullRequestCall(
            session_id=frame.session_id, vouch=frame.vouch, account_rights=frame.account_rights
        )
        if frame.session_id
        else None
    )
    service = WorkspaceService(
        lambda _ctx: _reader_at(reader_for_workspace, frame.workspace), pull_request_call
    )
    request_id = frame.request_id
    try:
        async for response in _call(service, frame.method, frame.request):
            yield HostWorkspaceReplyFrame(request_id, message=response.SerializeToString())
    except ConnectError as exc:
        yield HostWorkspaceReplyFrame(
            request_id,
            done=True,
            error_code=exc.code.value,
            error_message=exc.message,
            error_reason=exc.reason if isinstance(exc, PullRequestRefused) else None,
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
