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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.request import RequestContext
from google.protobuf.message import Message
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.api.workspace.v1alpha1.workspace_connect import (
    WorkspaceService as WorkspaceServiceMethods,
)
from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    GetCapabilitiesRequest,
    GetCapabilitiesResponse,
)
from omnigent.host.frames import HostWorkspaceCallFrame, HostWorkspaceReplyFrame, encode_host_frame
from omnigent.host.workspace_methods import WORKSPACE_METHODS, WorkspaceMethod
from omnigent.server.host_registry import HostConnection, HostRegistry

# Each reply must arrive within this; a whole ReadFile can take longer.
_REPLY_TIMEOUT_S = 30.0
# Served only while the workspace_content_search flag is on.
_CONTENT_SEARCH_METHOD = "SearchText"


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


class HostWorkspaceService(WorkspaceServiceMethods):
    """Implements ``WorkspaceService`` by forwarding each call to the session's host.

    Every method goes to the host, which answers UNIMPLEMENTED for those it
    doesn't serve and lists the ones it does in GetCapabilities.

    :param target: The session's host, or the error every call answers with
        when the session can't be served this way.
    :param content_search: Whether SearchText is served; when off, it is
        UNIMPLEMENTED and GetCapabilities omits it.
    """

    def __init__(self, target: HostTarget | ConnectError, *, content_search: bool) -> None:
        self._target = target
        self._content_search = content_search

    async def get_capabilities(
        self,
        request: GetCapabilitiesRequest,
        _ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse],
    ) -> GetCapabilitiesResponse:
        response = GetCapabilitiesResponse.FromString(
            await self._unary("GetCapabilities", request)
        )
        if not self._content_search and _CONTENT_SEARCH_METHOD in response.capabilities:
            names = [name for name in response.capabilities if name != _CONTENT_SEARCH_METHOD]
            response = GetCapabilitiesResponse(capabilities=names)
        return response

    def _check_served(self, method: WorkspaceMethod) -> None:
        if method.name == _CONTENT_SEARCH_METHOD and not self._content_search:
            raise ConnectError(Code.UNIMPLEMENTED, "Workspace content search is disabled")

    async def _unary(self, method: str, request: Message) -> bytes:
        messages = [message async for message in self._forward(method, request)]
        if not messages:
            raise ConnectError(Code.INTERNAL, "The session's host sent no response")
        return messages[0]

    async def _forward(self, method: str, request: Message) -> AsyncIterator[bytes]:
        """Send one call to the host and yield its serialized responses."""
        target = self._target
        if isinstance(target, ConnectError):
            raise target
        request_id = secrets.token_hex(8)
        replies: asyncio.Queue[HostWorkspaceReplyFrame] = asyncio.Queue()
        target.host.pending_workspace_calls[request_id] = replies
        # A caller disconnect doesn't stop the host's read (bounded by the 10 MiB cap).
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


def _host_forwarder(method: WorkspaceMethod) -> Callable[..., object]:
    """A HostWorkspaceService method that forwards ``method`` to the host."""
    if method.streaming:

        async def forward_stream(
            self: HostWorkspaceService, request: Message, _ctx: RequestContext
        ) -> AsyncIterator[Message]:
            self._check_served(method)
            async for message in self._forward(method.name, request):
                yield method.response_type.FromString(message)

        forward_stream.__name__ = method.python_name
        return forward_stream

    async def forward(
        self: HostWorkspaceService, request: Message, _ctx: RequestContext
    ) -> Message:
        self._check_served(method)
        return method.response_type.FromString(await self._unary(method.name, request))

    forward.__name__ = method.python_name
    return forward


for _method in WORKSPACE_METHODS.values():
    if _method.name != "GetCapabilities":
        setattr(HostWorkspaceService, _method.python_name, _host_forwarder(_method))


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
