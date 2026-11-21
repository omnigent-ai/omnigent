"""ACP-over-WebSocket client for ``grok agent serve``.

Minimal JSON-RPC client for the grok_native harness: ``initialize`` /
``session/new`` / ``session/load`` / ``session/prompt`` / ``session/cancel`` /
``session/close`` calls, with ``session/update`` notifications surfaced to a
callback so the forwarder can mirror items into the Omnigent conversation.

The handshake (live-verified) returns ``protocolVersion: 1``, session
``list/resume/close`` capabilities, MCP http+sse support, image prompts, and
the ``_meta["x.ai/hooks"]`` client-registration extension used for the policy
bridge. See docs/GROK_NATIVE_DESIGN.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = 1


class GrokAcpError(RuntimeError):
    """A JSON-RPC error returned by the serve endpoint or a transport failure."""


@dataclass
class GrokAcpClient:
    """One ACP-over-WS connection to a ``grok agent serve`` endpoint.

    Use :meth:`connect` to construct: it opens the socket, performs the
    ``initialize`` handshake, and returns a ready client. Call
    :meth:`set_update_handler` before ``session/new`` or ``session/prompt``
    so ``session/update`` notifications are routed to the forwarder.
    """

    ws: Any
    _next_id: int = 0
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _recv_task: asyncio.Task | None = None
    _update_handler: Callable[[dict[str, Any]], None] | None = None
    _permission_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    async def connect(
        cls,
        ws_url: str,
        *,
        timeout_s: float = 15.0,
        client_name: str = "omnigent",
        client_version: str = "0.1",
    ) -> GrokAcpClient:
        """Open the WS, run ``initialize``, and return the bound client."""
        import websockets

        ws = await websockets.connect(ws_url)
        client = cls(ws=ws)
        client._recv_task = asyncio.create_task(client._recv_loop(), name="grok-acp-recv")
        try:
            result = await client.request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": {"name": client_name, "version": client_version},
                },
                timeout_s=timeout_s,
            )
        except BaseException:
            client._recv_task.cancel()
            await ws.close()
            raise
        result = result or {}
        client.capabilities = result.get("agentCapabilities") or {}
        client.meta = result.get("_meta") or {}
        return client

    def set_update_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        """Set the ``session/update`` notification sink (forwarder)."""
        self._update_handler = handler

    def set_permission_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None,
    ) -> None:
        """Set the ``session/request_permission`` handler.

        Called with the request params; returns the ACP outcome dict
        (``{"outcome": {"selected": {"optionId": ...}}}``) or ``None`` to
        answer ``{"outcome": {"cancelled": null}}``.
        """
        self._permission_handler = handler

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 60.0
    ) -> Any:
        """Send a JSON-RPC request and await its result."""
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        )
        try:
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        )

    async def session_new(
        self, *, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> str:
        """Create a session; returns its session id."""
        result = await self.request("session/new", {"cwd": cwd, "mcpServers": mcp_servers or []})
        session_id = (result or {}).get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise GrokAcpError(f"session/new returned no sessionId: {result!r}")
        return session_id

    async def session_load(self, *, session_id: str, cwd: str) -> None:
        """Resume a persisted session (server must advertise ``loadSession``)."""
        if not self.capabilities.get("loadSession"):
            raise GrokAcpError("serve endpoint does not advertise loadSession")
        await self.request("session/load", {"sessionId": session_id, "cwd": cwd})

    async def session_prompt(self, *, session_id: str, text: str) -> dict[str, Any]:
        """Drive one user turn; returns the ``session/prompt`` result."""
        return await self.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            timeout_s=3600.0,
        )

    async def session_cancel(self, *, session_id: str) -> None:
        """Interrupt a running turn."""
        await self.notify("session/cancel", {"sessionId": session_id})

    async def session_close(self, *, session_id: str) -> None:
        """Close a session (x.ai ``sessionCapabilities.close``)."""
        await self.request("session/close", {"sessionId": session_id})

    async def _handle_permission_request(self, msg: dict[str, Any]) -> None:
        """Answer a server-initiated ``session/request_permission`` call."""
        req_id = msg["id"]
        outcome: dict[str, Any] = {"outcome": {"cancelled": None}}
        if self._permission_handler is not None:
            try:
                result = await self._permission_handler(msg.get("params") or {})
                if result is not None:
                    outcome = result
            except Exception:
                logger.exception("grok permission handler failed")
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": outcome}))

    async def close(self) -> None:
        """Tear the connection down."""
        if self._recv_task is not None:
            self._recv_task.cancel()
        with contextlib.suppress(Exception):
            await self.ws.close()

    async def _recv_loop(self) -> None:
        """Dispatch responses to pending futures; route notifications."""
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._pending.get(msg["id"])
                    if fut is None or fut.done():
                        continue
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(
                            GrokAcpError(
                                f"{err.get('message', 'rpc error')} (code {err.get('code')})"
                            )
                        )
                    else:
                        fut.set_result(msg.get("result"))
                elif msg.get("method") == "session/request_permission" and "id" in msg:
                    await self._handle_permission_request(msg)
                elif msg.get("method") in ("session/update", "_x.ai/session/update"):
                    if self._update_handler is not None:
                        try:
                            self._update_handler(msg.get("params") or {})
                        except Exception:
                            logger.exception("grok session/update handler failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("grok acp recv loop ended")
            # Fail all pending calls so waiters don't hang on a dead socket.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(GrokAcpError("websocket closed"))


__all__ = ["GrokAcpClient", "GrokAcpError"]
