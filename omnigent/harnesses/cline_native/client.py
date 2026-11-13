"""WebSocket client for the ``cline hub`` daemon.

Minimal client for the hub's JSON command/event protocol: ``client.register``
handshake, typed commands (``session.create`` / ``session.send_input`` /
``run.abort`` / ``approval.respond`` / ``mcp.servers.upsert`` / ``session.list``
/ ``session.restore``), with ``event`` frames surfaced to a callback so the
forwarder can mirror items into the Omnigent conversation.

Wire format (live-verified against cline 3.0.74): JSON frames
``{"kind": "command"|"reply"|"event", "envelope": {...}}`` over a WebSocket
authenticated with the ``cline-hub-auth.<token>`` subprotocol. Command
envelopes carry ``version: "v1"``, ``command``, ``requestId``, ``clientId``
(must equal the registered id or the hub answers
``client_authority_mismatch``), optional ``sessionId``, and ``payload``.
Replies match on ``requestId``; events broadcast to every registered client.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "v1"


class ClineHubError(RuntimeError):
    """A hub command error or a transport failure."""


@dataclass
class ClineHubClient:
    """One registered connection to a ``cline hub`` daemon.

    Use :meth:`connect` to construct: it opens the socket with the
    ``cline-hub-auth.<token>`` subprotocol, registers the client, and returns
    a ready client. Call :meth:`set_event_handler` before driving sessions so
    hub events are routed to the forwarder.
    """

    ws: Any
    client_id: str
    _next_id: itertools.count = field(default_factory=lambda: itertools.count(1))
    _pending: dict[str, asyncio.Future] = field(default_factory=dict)
    _recv_task: asyncio.Task | None = None
    _event_handler: Callable[[dict[str, Any]], None] | None = None
    registered: bool = False

    @classmethod
    async def connect(
        cls,
        ws_url: str,
        auth_token: str,
        *,
        client_id: str | None = None,
        workspace_root: str = "",
        cwd: str = "",
        timeout_s: float = 15.0,
    ) -> ClineHubClient:
        """Open the WS, run ``client.register``, and return the bound client."""
        import secrets

        # A shared client_id would collide on the hub's subscription key
        # (``clientId:sessionId``): a second connection's subscribe would be
        # deduped away and its events routed to the first. Default to unique.
        client_id = client_id or f"omnigent-{_pid()}-{secrets.token_hex(3)}"
        import websockets

        ws = await websockets.connect(
            ws_url,
            subprotocols=[websockets.Subprotocol(f"cline-hub-auth.{auth_token}")],
        )
        client = cls(ws=ws, client_id=client_id)
        client._recv_task = asyncio.create_task(client._recv_loop(), name="cline-hub-recv")
        try:
            await client.command(
                "client.register",
                {
                    "clientId": client_id,
                    "clientType": "core",
                    "displayName": "omnigent",
                    "transport": "native",
                    "actorKind": "client",
                    "capabilities": [],
                    "metadata": {"version": "omnigent", "pid": _pid()},
                    "workspaceContext": {"workspaceRoot": workspace_root, "cwd": cwd},
                },
                timeout_s=timeout_s,
            )
        except BaseException:
            client._recv_task.cancel()
            await ws.close()
            raise
        client.registered = True
        return client

    def set_event_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        """Set the hub-event sink (the forwarder)."""
        self._event_handler = handler

    async def subscribe(self, session_id: str | None = None) -> None:
        """Subscribe this connection to hub events (optionally one session).

        Events require an explicit top-level ``stream.subscribe`` frame;
        ``sessionId`` omitted means the global stream.
        """
        frame: dict[str, Any] = {
            "kind": "stream.subscribe",
            "clientId": self.client_id,
        }
        if session_id:
            frame["sessionId"] = session_id
        await self.ws.send(json.dumps(frame))

    async def command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        """Send a hub command and await its reply payload."""
        request_id = f"omni-{next(self._next_id)}"
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        envelope: dict[str, Any] = {
            "version": _PROTOCOL_VERSION,
            "command": command,
            "requestId": request_id,
            "clientId": self.client_id,
            "payload": payload or {},
        }
        if session_id:
            envelope["sessionId"] = session_id
        try:
            await self.ws.send(json.dumps({"kind": "command", "envelope": envelope}))
            reply = await asyncio.wait_for(fut, timeout_s)
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise ClineHubError(
                f"{command}: {error.get('code', 'unknown')}: {error.get('message', '')}"
            )
        return reply.get("payload") or {}

    async def session_create(
        self,
        *,
        workspace_root: str,
        cwd: str,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        auto_approve_tools: bool = False,
        tool_policies: dict[str, Any] | None = None,
        source: str = "omnigent",
    ) -> str:
        """Create a hub session; returns the vendor session id."""
        session_config: dict[str, Any] = {
            "cwd": cwd,
            "workspaceRoot": workspace_root,
            "systemPrompt": system_prompt or "",
            "mode": "act",
        }
        if provider:
            session_config["providerId"] = provider
        if model:
            session_config["modelId"] = model
        payload = await self.command(
            "session.create",
            {
                "workspaceRoot": workspace_root,
                "cwd": cwd,
                "sessionConfig": session_config,
                "metadata": {
                    "source": source,
                    "provider": provider,
                    "model": model,
                    "interactive": False,
                },
                "runtimeOptions": {
                    "mode": "act",
                    "autoApproveTools": auto_approve_tools,
                },
                "modelSelection": {"provider": provider, "model": model},
                "toolPolicies": tool_policies or {},
            },
            timeout_s=120.0,
        )
        session = payload.get("session")
        session_id = session.get("sessionId") if isinstance(session, dict) else None
        if not session_id:
            raise ClineHubError("session.create returned no session id")
        return str(session_id)

    async def session_resume(
        self,
        session_id: str,
        *,
        workspace_root: str,
        cwd: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Resume a saved session; returns the vendor session id."""
        session_config: dict[str, Any] = {
            "sessionId": session_id,
            "cwd": cwd,
            "workspaceRoot": workspace_root,
        }
        if provider:
            session_config["providerId"] = provider
        if model:
            session_config["modelId"] = model
        payload = await self.command(
            "session.resume",
            {"sessionConfig": session_config},
            session_id=session_id,
            timeout_s=120.0,
        )
        session = payload.get("session")
        resumed = session.get("sessionId") if isinstance(session, dict) else None
        return str(resumed or session_id)

    async def send_input(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Submit one user turn; replies when the run settles or times out."""
        request: dict[str, Any] = {"prompt": prompt, "mode": "act"}
        kwargs: dict[str, Any] = {"session_id": session_id}
        if timeout_s is not None:
            kwargs["timeout_s"] = timeout_s
        return await self.command("session.send_input", request, **kwargs)

    async def abort(self, session_id: str) -> None:
        """Abort the running turn on a session."""
        await self.command("run.abort", {"sessionId": session_id}, session_id=session_id)

    async def respond_approval(
        self, approval_id: str, approved: bool, *, reason: str | None = None
    ) -> None:
        """Answer an ``approval.requested`` prompt."""
        payload: dict[str, Any] = {"approvalId": approval_id, "approved": approved}
        if reason:
            payload["payload"] = {"reason": reason}
        await self.command("approval.respond", payload)

    async def upsert_mcp_stdio(
        self,
        name: str,
        command: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Register (or replace) a stdio MCP server in the hub settings."""
        await self.command(
            "mcp.servers.upsert",
            {
                "name": name,
                "transportType": "stdio",
                "command": command,
                "args": args,
                "env": env or {},
            },
        )

    async def close(self) -> None:
        """Tear down the receive task and socket."""
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        with contextlib.suppress(Exception):
            await self.ws.close()

    async def _recv_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("kind")
                envelope = msg.get("envelope")
                if not isinstance(envelope, dict):
                    continue
                if kind == "reply":
                    fut = self._pending.get(envelope.get("requestId") or "")
                    if fut is not None and not fut.done():
                        fut.set_result(envelope)
                elif kind == "event" and self._event_handler is not None:
                    try:
                        self._event_handler(envelope)
                    except Exception:
                        logger.exception("cline hub event handler raised")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cline hub recv loop ended")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ClineHubError("hub connection closed"))


def _pid() -> int:
    import os

    return os.getpid()


__all__ = ["ClineHubClient", "ClineHubError"]
