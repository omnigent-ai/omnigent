"""Async stdio JSON-RPC 2.0 client for ``cursor-agent acp``.

``cursor-agent acp`` runs the Cursor agent as an `Agent Client Protocol
<https://cursor.com/docs/cli/acp>`_ server over stdio: newline-delimited JSON-RPC
2.0. This client is the transport the ``CursorNativeExecutor`` drives — the
cursor-native analog of codex-native's
:class:`~omnigent.codex_native_app_server.CodexAppServerClient` (codex speaks the
same shape over a WebSocket; cursor speaks it over stdio).

Lifecycle per Omnigent conversation:

1. :meth:`start` — spawn ``cursor-agent acp`` and ``initialize``.
2. :meth:`new_session` (fresh) or :meth:`load_session` (resume) → an ACP session id.
3. :meth:`prompt` — send ``session/prompt`` and stream the ``session/update``
   notifications (assistant text, reasoning, tool calls) until the turn ends.
4. :meth:`close` — terminate the subprocess.

The agent issues client→agent requests mid-turn (``session/request_permission``,
``fs/read_text_file``, ``fs/write_text_file``); :meth:`_handle_agent_request`
answers them. Permission is auto-allowed for now — a policy bridge lands in a
later change.

Auth is ambient: ``cursor-agent`` reads the ``cursor-agent login`` credentials
under ``$HOME/.cursor``, so no API key is threaded here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ACP protocol version this client speaks. cursor-agent reports the same value
# from ``initialize`` (verified against cursor-agent 2026.06.x).
_PROTOCOL_VERSION = 1

# Bound on how long ``initialize`` / ``session/new`` may take before we give up
# on a wedged subprocess (the prompt stream itself is unbounded — model turns
# can run for minutes).
_HANDSHAKE_TIMEOUT_S = 30.0


class CursorAcpError(RuntimeError):
    """An ACP request failed or the agent returned a JSON-RPC error."""


@dataclass
class _TurnEnd:
    """Sentinel pushed onto a session's update queue when a prompt turn ends."""

    stop_reason: str | None
    error: str | None


class CursorAcpClient:
    """Drive one ``cursor-agent acp`` subprocess over stdio JSON-RPC."""

    def __init__(
        self,
        *,
        binary: str = "cursor-agent",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Create a client (does not spawn until :meth:`start`).

        :param binary: The cursor-agent executable name or path.
        :param cwd: Working directory the agent operates in; defaults to the
            process cwd.
        :param env: Environment for the subprocess; ``None`` inherits the
            process environment (so the ambient ``$HOME/.cursor`` login applies).
        """
        self._binary = binary
        self._cwd = cwd or os.getcwd()
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0
        # request id -> future, for client→agent requests we await (initialize,
        # session/new, session/load). Prompt requests are tracked separately
        # because their completion is surfaced through the update queue.
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # session id -> queue of ``session/update`` payloads (and a terminal
        # :class:`_TurnEnd`). One ordered channel per session keeps updates and
        # the turn-end signal in stream order.
        self._update_queues: dict[str, asyncio.Queue[dict[str, Any] | _TurnEnd]] = {}
        # prompt request id -> session id, so the reader routes the prompt
        # response onto the right session queue as a _TurnEnd.
        self._prompt_session: dict[int, str] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False
        #: Stop reason of the most recently completed prompt turn, e.g.
        #: ``"end_turn"`` or ``"max_tokens"``.
        self.last_stop_reason: str | None = None

    async def start(self) -> None:
        """Spawn ``cursor-agent acp`` and complete the ``initialize`` handshake."""
        resolved = shutil.which(self._binary) or self._binary
        self._proc = await asyncio.create_subprocess_exec(
            resolved,
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await asyncio.wait_for(
            self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                },
            ),
            timeout=_HANDSHAKE_TIMEOUT_S,
        )

    async def new_session(self, *, mcp_servers: list[dict[str, Any]] | None = None) -> str:
        """Create a fresh ACP session and return its id.

        :param mcp_servers: ACP ``mcpServers`` entries to attach (host-tool
            relay; unused until the MCP-relay PR).
        """
        result = await asyncio.wait_for(
            self._request(
                "session/new",
                {"cwd": self._cwd, "mcpServers": mcp_servers or []},
            ),
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise CursorAcpError(f"session/new returned no sessionId: {result!r}")
        self._update_queues[session_id] = asyncio.Queue()
        return session_id

    async def load_session(
        self, session_id: str, *, mcp_servers: list[dict[str, Any]] | None = None
    ) -> None:
        """Resume a previously created ACP session by id."""
        self._update_queues.setdefault(session_id, asyncio.Queue())
        await asyncio.wait_for(
            self._request(
                "session/load",
                {
                    "sessionId": session_id,
                    "cwd": self._cwd,
                    "mcpServers": mcp_servers or [],
                },
            ),
            timeout=_HANDSHAKE_TIMEOUT_S,
        )

    async def prompt(
        self, session_id: str, blocks: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """Send ``session/prompt`` and yield ``session/update`` payloads until the turn ends.

        On completion, :attr:`last_stop_reason` holds the turn's stop reason.

        :param session_id: The ACP session id from :meth:`new_session`.
        :param blocks: ACP prompt content blocks, e.g.
            ``[{"type": "text", "text": "..."}]``.
        :raises CursorAcpError: If the agent reports an error for the turn.
        """
        queue = self._update_queues.get(session_id)
        if queue is None:
            raise CursorAcpError(f"unknown ACP session {session_id!r}")
        self._next_id += 1
        request_id = self._next_id
        self._prompt_session[request_id] = session_id
        self.last_stop_reason = None
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": blocks},
            }
        )
        while True:
            item = await queue.get()
            if isinstance(item, _TurnEnd):
                self.last_stop_reason = item.stop_reason
                if item.error is not None:
                    raise CursorAcpError(f"session/prompt failed: {item.error}")
                return
            yield item

    async def cancel(self, session_id: str) -> None:
        """Best-effort ``session/cancel`` for the in-flight turn (a notification)."""
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort
            logger.debug("cursor-acp session/cancel failed: %s", exc)

    async def close(self) -> None:
        """Terminate the subprocess and cancel the reader tasks (idempotent)."""
        if self._closed:
            return
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        # Fail any still-pending request so awaiters don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CursorAcpError("cursor-agent acp closed"))
        self._pending.clear()

    # -- internals ----------------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a client→agent request and await its result."""
        self._next_id += 1
        request_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await fut

    async def _send(self, obj: dict[str, Any]) -> None:
        """Write one newline-delimited JSON-RPC message to the agent's stdin."""
        if self._proc is None or self._proc.stdin is None:
            raise CursorAcpError("cursor-agent acp is not running")
        data = (json.dumps(obj) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Dispatch every message the agent writes to stdout until EOF."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning("cursor-acp non-JSON stdout: %r", stripped[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        finally:
            # Unblock any awaiters on EOF / reader exit.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CursorAcpError("cursor-agent acp stream ended"))
            self._pending.clear()
            for queue in self._update_queues.values():
                queue.put_nowait(_TurnEnd(stop_reason=None, error="stream ended"))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route one parsed message: notification, agent request, or response."""
        method = msg.get("method")
        if method is not None and "id" in msg:
            await self._handle_agent_request(msg)
            return
        if method is not None:
            if method == "session/update":
                params = msg.get("params", {})
                session_id = params.get("sessionId")
                queue = self._update_queues.get(session_id)
                if queue is not None:
                    queue.put_nowait(params.get("update", {}))
            # Other notifications (e.g. lifecycle pings) are ignored.
            return
        # A response to one of our client→agent requests.
        request_id = msg.get("id")
        if request_id in self._prompt_session:
            session_id = self._prompt_session.pop(request_id)
            queue = self._update_queues.get(session_id)
            if "error" in msg:
                end = _TurnEnd(stop_reason=None, error=json.dumps(msg["error"]))
            else:
                result = msg.get("result") or {}
                end = _TurnEnd(stop_reason=result.get("stopReason"), error=None)
            if queue is not None:
                queue.put_nowait(end)
            return
        fut = self._pending.pop(request_id, None)
        if fut is not None and not fut.done():
            if "error" in msg:
                fut.set_exception(CursorAcpError(json.dumps(msg["error"])))
            else:
                fut.set_result(msg.get("result") or {})

    async def _handle_agent_request(self, msg: dict[str, Any]) -> None:
        """Answer an agent→client request so the turn isn't blocked.

        ``session/request_permission`` is auto-allowed (policy bridge is a later
        PR). ``fs/*`` are served against the real filesystem. Unknown requests
        get an empty result rather than an error so a future ACP method does not
        wedge a turn.
        """
        method = msg["method"]
        request_id = msg["id"]
        params = msg.get("params", {})
        try:
            if method == "session/request_permission":
                result = _auto_allow_permission(params)
            elif method == "fs/read_text_file":
                result = _read_text_file(params)
            elif method == "fs/write_text_file":
                result = _write_text_file(params)
            else:
                logger.debug("cursor-acp unhandled agent request: %s", method)
                result = {}
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:  # noqa: BLE001 — surface to the agent as a JSON-RPC error
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            )

    async def _drain_stderr(self) -> None:
        """Forward the subprocess stderr to the log at debug level."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug("cursor-acp stderr: %s", line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise


def _auto_allow_permission(params: dict[str, Any]) -> dict[str, Any]:
    """Select an "allow" option for a ``session/request_permission`` request.

    Prefers an option whose ``kind`` allows (``allow_once`` / ``allow_always``)
    or whose id mentions "allow"; falls back to the first offered option.
    """
    options = params.get("options") or []
    chosen: str | None = None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind", ""))
        opt_id = str(opt.get("optionId", ""))
        if kind in ("allow_once", "allow_always") or "allow" in opt_id.lower():
            chosen = opt_id
            break
    if chosen is None and options and isinstance(options[0], dict):
        chosen = str(options[0].get("optionId", ""))
    if not chosen:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": chosen}}


def _read_text_file(params: dict[str, Any]) -> dict[str, Any]:
    """Serve an ACP ``fs/read_text_file`` request from the real filesystem."""
    path = params.get("path")
    if not isinstance(path, str) or not path:
        raise CursorAcpError("fs/read_text_file missing path")
    text = Path(path).read_text(encoding="utf-8")
    return {"content": text}


def _write_text_file(params: dict[str, Any]) -> dict[str, Any]:
    """Serve an ACP ``fs/write_text_file`` request to the real filesystem."""
    path = params.get("path")
    content = params.get("content", "")
    if not isinstance(path, str) or not path:
        raise CursorAcpError("fs/write_text_file missing path")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content if isinstance(content, str) else "", encoding="utf-8")
    return {}
