"""The workspace API through the server, for clients that can't reach the host.

The server speaks the API (Connect, gRPC-Web, gRPC) itself and forwards each
call to the session's host as serialized messages over the host tunnel, where
:func:`omnigent.host.workspace_service.serve_tunnel_call` answers it with the
API's one implementation. The server authorizes the caller and picks the
workspace; the host never sees client input other than the request message.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.request import RequestContext
from google.protobuf.message import Message
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    GetCapabilitiesRequest,
    GetCapabilitiesResponse,
    ListDirectoryRequest,
    ListDirectoryResponse,
    ReadFileRequest,
    ReadFileResponse,
)
from omnigent.host.frames import HostWorkspaceCallFrame, HostWorkspaceReplyFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

# Each reply must arrive within this; a whole ReadFile can take longer.
_REPLY_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class HostTarget:
    """Where a session's workspace calls go.

    :param registry: Registry that sends frames to the host.
    :param host: The session's connected host.
    :param workspace: The session's recorded workspace on that host.
    """

    registry: HostRegistry
    host: HostConnection
    workspace: str


class HostWorkspaceService:
    """Implements ``WorkspaceService`` by forwarding each call to the session's host.

    :param target: The session's host, or the error every call answers with
        when the session can't be served this way.
    """

    def __init__(self, target: HostTarget | ConnectError) -> None:
        self._target = target

    async def get_capabilities(
        self,
        request: GetCapabilitiesRequest,
        _ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse],
    ) -> GetCapabilitiesResponse:
        return GetCapabilitiesResponse.FromString(await self._unary("GetCapabilities", request))

    async def list_directory(
        self,
        request: ListDirectoryRequest,
        _ctx: RequestContext[ListDirectoryRequest, ListDirectoryResponse],
    ) -> ListDirectoryResponse:
        return ListDirectoryResponse.FromString(await self._unary("ListDirectory", request))

    async def read_file(
        self,
        request: ReadFileRequest,
        _ctx: RequestContext[ReadFileRequest, ReadFileResponse],
    ) -> AsyncIterator[ReadFileResponse]:
        async for message in self._forward("ReadFile", request):
            yield ReadFileResponse.FromString(message)

    async def _unary(self, method: str, request: Message) -> bytes:
        messages = [message async for message in self._forward(method, request)]
        return messages[0] if messages else b""

    async def _forward(self, method: str, request: Message) -> AsyncIterator[bytes]:
        """Send one call to the host and yield its serialized responses."""
        target = self._target
        if isinstance(target, ConnectError):
            raise target
        request_id = secrets.token_hex(8)
        replies: asyncio.Queue[HostWorkspaceReplyFrame] = asyncio.Queue()
        target.host.pending_workspace_calls[request_id] = replies
        # ponytail: a caller that disconnects mid-stream leaves the host reading
        # to the end (at most the 10 MiB read cap); add a cancel frame if needed.
        try:
            call = HostWorkspaceCallFrame(
                request_id, target.workspace, method, request.SerializeToString()
            )
            try:
                target.registry.send_text(target.host, encode_host_frame(call))
            except ConnectionError as exc:
                raise ConnectError(Code.UNAVAILABLE, "The session's host disconnected") from exc
            while True:
                try:
                    reply = await asyncio.wait_for(replies.get(), timeout=_REPLY_TIMEOUT_S)
                except TimeoutError as exc:
                    raise ConnectError(
                        Code.DEADLINE_EXCEEDED, "The session's host did not answer"
                    ) from exc
                if reply.message is not None:
                    yield reply.message
                if reply.done:
                    if reply.error_code:
                        raise ConnectError(_code(reply.error_code), reply.error_message or "")
                    return
        finally:
            target.host.pending_workspace_calls.pop(request_id, None)


def _code(value: str) -> Code:
    try:
        return Code(value)
    except ValueError:
        return Code.UNKNOWN


class ConnectCallResponse(Response):
    """Hands a request to a Connect ASGI app, addressed as ``/{service}/{method}``.

    Lets a FastAPI route authorize first, then let the Connect app speak
    whichever protocol the client chose.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        super().__init__()
        self._app = app
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        call_scope = {
            **scope,
            "path": self._path,
            "raw_path": self._path.encode(),
            "root_path": "",
        }
        await self._app(call_scope, receive, send)
