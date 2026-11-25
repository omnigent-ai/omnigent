"""The workspace API through the server, for clients that can't reach the host.

The server speaks the API (Connect, gRPC-Web, gRPC) itself and forwards each
call to the session's host as serialized messages over the host tunnel, where
:func:`omnigent.host.workspace_service.serve_tunnel_call` answers it with the
API's one implementation. The server authorizes the caller and picks the
workspace; the host never sees client input other than the request message.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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
    GetPullRequestResponse,
)
from omnigent.host.frames import (
    HostWorkspaceCallFrame,
    HostWorkspaceReplyFrame,
    PullRequestVouch,
    encode_host_frame,
)
from omnigent.host.workspace_methods import WORKSPACE_METHODS, WorkspaceMethod
from omnigent.host.workspace_payloads import payload_dict, payload_struct
from omnigent.server.host_registry import HostConnection, HostRegistry

# Each reply must arrive within this; a whole ReadFile can take longer.
_REPLY_TIMEOUT_S = 30.0
# GetCapabilities lists features in lower snake case and methods in PascalCase.
_FEATURE_NAME = re.compile(r"[a-z0-9_.]+")
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


@dataclass(frozen=True)
class PullRequestRoute:
    """How the server takes part in a session's pull request reads, as its REST routes do.

    :param session_id: The session the caller was authorized for, from the call's URL.
    :param vouch: Returns the server's vouch for the PR a call names (``""`` for the
        session's first), or ``None`` before the server stores the session's PRs.
        Raises :class:`ConnectError` FAILED_PRECONDITION for a PR the session doesn't link.
    :param overview: Stores the PRs a GetPullRequest payload reports and answers with
        the server's list, given the time the call was sent.
    """

    session_id: str
    vouch: Callable[[str], Awaitable[PullRequestVouch | None]]
    overview: Callable[[dict[str, Any], float], Awaitable[dict[str, Any]]]


class HostWorkspaceService(WorkspaceServiceMethods):
    """Implements ``WorkspaceService`` by forwarding each call to the session's host.

    Every method goes to the host, which answers UNIMPLEMENTED for those it
    doesn't serve and lists the ones it does in GetCapabilities.

    :param target: The session's host, or the error every call answers with
        when the session can't be served this way.
    :param content_search: Whether SearchText is served; when off, it is
        UNIMPLEMENTED and GetCapabilities omits it.
    :param pull_requests: The server's part in pull request reads; without it, they
        are UNIMPLEMENTED and GetCapabilities omits them.
    :param callable_methods: The methods the caller may call; GetCapabilities omits
        the others. ``None`` lists whatever the host lists.
    """

    def __init__(
        self,
        target: HostTarget | ConnectError,
        *,
        content_search: bool,
        pull_requests: PullRequestRoute | None = None,
        callable_methods: frozenset[str] | None = None,
    ) -> None:
        self._target = target
        self._content_search = content_search
        self._pull_requests = pull_requests
        self._callable_methods = callable_methods

    async def get_capabilities(
        self,
        request: GetCapabilitiesRequest,
        _ctx: RequestContext[GetCapabilitiesRequest, GetCapabilitiesResponse],
    ) -> GetCapabilitiesResponse:
        # The host lists pull request methods only to a call that names their session.
        route = self._pull_requests
        response = GetCapabilitiesResponse.FromString(
            await self._unary(
                "GetCapabilities", request, session_id=route.session_id if route else None
            )
        )
        names = [name for name in response.capabilities if self._lists(name)]
        if names != list(response.capabilities):
            response = GetCapabilitiesResponse(capabilities=names)
        return response

    def _lists(self, name: str) -> bool:
        """Whether GetCapabilities passes on a method or feature name the host listed."""
        method = WORKSPACE_METHODS.get(name)
        if method is None:
            # A newer host's method this server has no gate for would be a 404 here.
            return _FEATURE_NAME.fullmatch(name) is not None
        if name == _CONTENT_SEARCH_METHOD and not self._content_search:
            return False
        if method.pull_request and self._pull_requests is None:
            return False
        return self._callable_methods is None or name in self._callable_methods

    async def _pull_request_call(self, method: WorkspaceMethod, request: Message) -> Message:
        """Forward a pull request read with the session and the server's vouch."""
        route = self._pull_requests
        if route is None:
            raise ConnectError(Code.UNIMPLEMENTED, "Pull requests aren't served on this route")
        # The vouch comes from the PRs the server stores, never from the request message.
        vouch = await route.vouch(getattr(request, "pr_url", ""))
        sent_at = time.time()
        message = await self._unary(method.name, request, session_id=route.session_id, vouch=vouch)
        if method.name != "GetPullRequest":
            return method.response_type.FromString(message)
        overview = GetPullRequestResponse.FromString(message)
        payload = await route.overview(payload_dict(overview.payload), sent_at)
        return GetPullRequestResponse(payload=payload_struct(payload))

    def _check_served(self, method: WorkspaceMethod) -> None:
        if method.name == _CONTENT_SEARCH_METHOD and not self._content_search:
            raise ConnectError(Code.UNIMPLEMENTED, "Workspace content search is disabled")

    async def _unary(
        self,
        method: str,
        request: Message,
        *,
        session_id: str | None = None,
        vouch: PullRequestVouch | None = None,
    ) -> bytes:
        messages = [
            message
            async for message in self._forward(method, request, session_id=session_id, vouch=vouch)
        ]
        if not messages:
            raise ConnectError(Code.INTERNAL, "The session's host sent no response")
        return messages[0]

    async def _forward(
        self,
        method: str,
        request: Message,
        *,
        session_id: str | None = None,
        vouch: PullRequestVouch | None = None,
    ) -> AsyncIterator[bytes]:
        """Send one call to the host and yield its serialized responses.

        :param session_id: The authorized session, sent on pull request calls only.
        :param vouch: The server's vouch for a pull request call's PR.
        """
        target = self._target
        if isinstance(target, ConnectError):
            raise target
        request_id = secrets.token_hex(8)
        replies: asyncio.Queue[HostWorkspaceReplyFrame] = asyncio.Queue()
        target.host.pending_workspace_calls[request_id] = replies
        # A caller disconnect doesn't stop the host's read (bounded by the 10 MiB cap).
        try:
            call = HostWorkspaceCallFrame(
                request_id,
                target.workspace,
                method,
                request.SerializeToString(),
                session_id=session_id,
                vouch=vouch,
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
        if method.pull_request:
            return await self._pull_request_call(method, request)
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
