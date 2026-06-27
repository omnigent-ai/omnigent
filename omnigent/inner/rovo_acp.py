"""Minimal ACP (Agent Client Protocol) client over an asyncio subprocess.

This module drives ``acli rovodev acp`` — Rovo Dev's ACP server mode — over
stdio using JSON-RPC 2.0. It is intentionally thin: it knows how to perform the
ACP handshake (``initialize``), open a session (``session/new``), send a prompt
(``session/prompt``), cancel a turn (``session/cancel``), and surface the
streamed ``session/update`` notifications to a caller-supplied callback.

The higher-level :class:`omnigent.inner.rovo_executor.RovoExecutor` translates
the raw ACP updates this client yields into Omnigent
:class:`~omnigent.inner.executor.ExecutorEvent` instances.

Transport shape (framed JSON-RPC, one JSON object per line) mirrors the
in-repo precedent ``_CodexAppServerSession`` in ``codex_executor.py``; ACP is
simply a different JSON-RPC dialect over the same stdio transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

logger = logging.getLogger(__name__)

# ACP protocol version this client implements (matches the value Rovo Dev's
# ``acp`` server advertises from ``initialize``).
ACP_PROTOCOL_VERSION = 1

# Type alias for the JSON-shaped ACP boundary. Values are heterogeneous JSON
# decoded straight off the wire; callers narrow per ``method`` / field with
# ``isinstance`` before use. ``Any`` is the documented escape hatch for opaque
# foreign-protocol JSON (see [tool.mypy] disallow_any_explicit rationale and
# the same pattern in ``codex_executor.py``).
JsonObj: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Callback invoked for each ``session/update`` notification's ``update`` payload.
UpdateHandler = Callable[[JsonObj], Awaitable[None]]

# Callback invoked when the server makes a *request* of the client (e.g.
# ``session/request_permission``). Returns the JSON-RPC ``result`` payload.
RequestHandler = Callable[[str, JsonObj], Awaitable[JsonObj]]

_DEFAULT_TURN_TIMEOUT_SECONDS = 600.0


class AcpError(RuntimeError):
    """Raised when the ACP server returns a JSON-RPC error or the transport fails."""


class AcpProcessExited(AcpError):
    """Raised when the ACP subprocess exits unexpectedly mid-session."""


def default_acp_command(
    *,
    acli_path: str | None = None,
    config_file: str | None = None,
    site_url: str | None = None,
) -> list[str]:
    """Build the ``acli rovodev acp`` command line.

    :param acli_path: Path to the ``acli`` binary. ``None`` uses ``"acli"``
        from ``PATH``.
    :param config_file: Optional ``--config-file`` value (Rovo Dev defaults to
        ``~/.rovodev/config.yml`` when omitted).
    :param site_url: Optional ``--site-url`` value.
    :returns: Argument vector suitable for :func:`asyncio.create_subprocess_exec`.
    """
    cmd = [acli_path or "acli", "rovodev", "acp"]
    if config_file:
        cmd += ["--config-file", config_file]
    if site_url:
        cmd += ["--site-url", site_url]
    return cmd


class AcpClient:
    """JSON-RPC 2.0 client for an ACP agent spoken over subprocess stdio.

    Lifecycle::

        client = AcpClient(command=default_acp_command())
        await client.start()
        await client.initialize()
        session_id = await client.session_new(cwd=os.getcwd())
        stop_reason = await client.session_prompt(
            session_id, [{"type": "text", "text": "hi"}], on_update=handler
        )
        await client.close()

    The client owns one subprocess. A background reader loop demultiplexes
    incoming lines into three categories: responses to our requests (matched by
    ``id``), notifications (``session/update`` → ``on_update``), and inbound
    requests from the server (``session/request_permission`` etc. → the
    ``request_handler``).
    """

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        request_handler: RequestHandler | None = None,
    ) -> None:
        """Create an ACP client (does not spawn the process; call :meth:`start`).

        :param command: Argument vector, e.g. from :func:`default_acp_command`.
        :param env: Environment for the subprocess. ``None`` inherits the
            current process environment.
        :param cwd: Working directory for the subprocess.
        :param request_handler: Optional handler for server→client requests.
            When ``None``, such requests are auto-answered with an empty result.
        """
        self._command = command
        self._env = env
        self._cwd = cwd
        self._request_handler = request_handler

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._next_id = 0
        self._pending: dict[int, asyncio.Future[JsonObj]] = {}
        # Per-active-prompt update handler, keyed by session id.
        self._update_handlers: dict[str, UpdateHandler] = {}
        self._closed = False
        self._stderr_tail: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the ACP subprocess and begin the reader loop."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,  # None → inherits parent env
            cwd=self._cwd,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def close(self) -> None:
        """Terminate the subprocess and cancel background tasks."""
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpProcessExited("ACP client closed"))
        self._pending.clear()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # -- low-level JSON-RPC -------------------------------------------------

    async def _write(self, payload: JsonObj) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpProcessExited("ACP subprocess not started")
        line = json.dumps(payload) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await proc.stdin.drain()

    async def request(self, method: str, params: JsonObj | None = None) -> JsonObj:
        """Send a JSON-RPC request and await its result.

        :param method: ACP method name, e.g. ``"session/new"``.
        :param params: Method params object.
        :returns: The ``result`` object from the matching response.
        :raises AcpError: If the server returns an ``error`` member.
        :raises AcpProcessExited: If the subprocess dies before responding.
        """
        if self._closed:
            raise AcpProcessExited("ACP client is closed")
        self._next_id += 1
        request_id = self._next_id
        fut: asyncio.Future[JsonObj] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        msg: JsonObj = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)
        return await fut

    async def notify(self, method: str, params: JsonObj | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: JsonObj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    # -- high-level ACP methods --------------------------------------------

    async def initialize(self) -> JsonObj:
        """Perform the ACP ``initialize`` handshake.

        :returns: The server's capabilities/result object (contains
            ``protocolVersion``, ``agentCapabilities``, ``authMethods``).
        """
        return await self.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
        )

    async def session_new(self, *, cwd: str, mcp_servers: list[JsonObj] | None = None) -> JsonObj:
        """Open a new ACP session.

        :param cwd: Working directory the agent should operate in.
        :param mcp_servers: Optional MCP servers to advertise to the agent
            (the "MCP over ACP" tool-bridge channel). Defaults to none.
        :returns: The full ``session/new`` result. Includes ``sessionId`` and
            a ``models`` object of the shape
            ``{"availableModels": [{"modelId": str, "name": str}, ...],
            "currentModelId": str}``.
        """
        return await self.request(
            "session/new",
            {"cwd": cwd, "mcpServers": mcp_servers or []},
        )

    async def session_set_model(self, session_id: str, model_id: str) -> None:
        """Select the model for an existing session.

        :param session_id: Session id from :meth:`session_new`.
        :param model_id: One of the ``modelId`` values advertised in the
            ``session/new`` result's ``models.availableModels``.
        """
        await self.request(
            "session/set_model",
            {"sessionId": session_id, "modelId": model_id},
        )

    async def session_prompt(
        self,
        session_id: str,
        prompt: list[JsonObj],
        *,
        on_update: UpdateHandler,
        timeout: float = _DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> str:
        """Send a prompt turn and stream updates until the turn completes.

        :param session_id: Session id from :meth:`session_new`.
        :param prompt: ACP content blocks, e.g.
            ``[{"type": "text", "text": "..."}]``.
        :param on_update: Async callback invoked with each ``update`` payload
            from ``session/update`` notifications for this session.
        :param timeout: Max seconds to await turn completion.
        :returns: The ACP ``stopReason`` (e.g. ``"end_turn"``).
        """
        self._update_handlers[session_id] = on_update
        try:
            result = await asyncio.wait_for(
                self.request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": prompt},
                ),
                timeout=timeout,
            )
        finally:
            self._update_handlers.pop(session_id, None)
        stop_reason = result.get("stopReason")
        return str(stop_reason) if stop_reason is not None else "end_turn"

    async def session_cancel(self, session_id: str) -> None:
        """Request cancellation of the active turn for ``session_id``."""
        with contextlib.suppress(Exception):
            await self.notify("session/cancel", {"sessionId": session_id})

    # -- background loops ---------------------------------------------------

    async def _reader_loop(self) -> None:
        """Read newline-framed JSON-RPC messages and dispatch them."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break  # EOF: subprocess closed stdout
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("rovo-acp: non-JSON line ignored: %s", line[:200])
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:
            logger.exception("rovo-acp: reader loop error")
        finally:
            self._fail_pending(AcpProcessExited("ACP subprocess stdout closed"))

    async def _dispatch(self, msg: JsonObj) -> None:
        """Route one decoded message to a pending request, handler, or notify."""
        msg_id = msg.get("id")
        method = msg.get("method")

        # 1) Response to one of our requests (has id, no method).
        if msg_id is not None and method is None:
            fut = self._pending.pop(int(msg_id), None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(AcpError(_format_rpc_error(err)))
            else:
                fut.set_result(msg.get("result") or {})
            return

        # 2) Inbound request from the server (has id AND method).
        if msg_id is not None and method is not None:
            await self._handle_server_request(int(msg_id), str(method), msg.get("params") or {})
            return

        # 3) Notification (method, no id).
        if method is not None:
            await self._handle_notification(str(method), msg.get("params") or {})

    async def _handle_notification(self, method: str, params: JsonObj) -> None:
        if method == "session/update":
            session_id = params.get("sessionId")
            update = params.get("update")
            if not isinstance(update, dict):
                return
            handler = self._update_handlers.get(str(session_id))
            if handler is not None:
                await handler(update)

    async def _handle_server_request(self, request_id: int, method: str, params: JsonObj) -> None:
        # A caller-supplied handler wins, when present.
        if self._request_handler is not None:
            result: JsonObj = {}
            with contextlib.suppress(Exception):
                result = await self._request_handler(method, params)
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
            return

        # Built-in defaults for the ACP requests Rovo makes during a turn.
        if method == "session/request_permission":
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": _auto_allow_permission(params),
                }
            )
            return

        # Unknown server→client request: return an empty result so the agent
        # isn't left waiting (matches the previous permissive behaviour).
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": {}})

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _stderr_loop(self) -> None:
        """Drain stderr, keeping a short tail for diagnostics."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-50]
                    logger.debug("rovo-acp[stderr]: %s", text)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001 - diagnostics only
            pass

    @property
    def stderr_tail(self) -> str:
        """Recent stderr output, for error messages."""
        return "\n".join(self._stderr_tail)


def _auto_allow_permission(params: JsonObj) -> JsonObj:
    """Build a ``session/request_permission`` response that allows the call.

    Rovo runs headless under Omnigent (no interactive ACP permission prompt),
    and Omnigent enforces its own policy layer around tool dispatch, so we
    auto-select an "allow" option here. The ACP permission request carries an
    ``options`` list, each with an ``optionId`` and a ``kind`` such as
    ``allow_once`` / ``allow_always`` / ``reject_once`` / ``reject_always``.

    We prefer an ``allow_once`` option, then any ``allow*`` kind, then fall
    back to the first option's id. The response shape is
    ``{"outcome": {"outcome": "selected", "optionId": <id>}}``.

    :param params: The ``session/request_permission`` params.
    :returns: A JSON-RPC result object granting the permission.
    """
    options = params.get("options")
    option_id: str | None = None
    if isinstance(options, list) and options:
        by_kind: dict[str, str] = {}
        for opt in options:
            if isinstance(opt, dict) and opt.get("optionId"):
                kind = str(opt.get("kind", ""))
                by_kind.setdefault(kind, str(opt["optionId"]))
        option_id = (
            by_kind.get("allow_once")
            or by_kind.get("allow_always")
            or next(
                (oid for kind, oid in by_kind.items() if kind.startswith("allow")),
                None,
            )
        )
        if option_id is None:
            first = options[0]
            if isinstance(first, dict) and first.get("optionId"):
                option_id = str(first["optionId"])
    if option_id is None:
        # No options offered — signal a generic allow outcome.
        return {"outcome": {"outcome": "selected"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _format_rpc_error(err: object) -> str:
    """Render a JSON-RPC ``error`` member as a readable string."""
    if isinstance(err, dict):
        code = err.get("code")
        message = err.get("message", "unknown error")
        data = err.get("data")
        out = f"ACP error {code}: {message}" if code is not None else f"ACP error: {message}"
        if data:
            out += f" ({json.dumps(data)[:300]})"
        return out
    return f"ACP error: {err}"
