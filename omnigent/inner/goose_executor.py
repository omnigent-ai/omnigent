"""GooseExecutor: run agents through Block's Goose in ACP mode.

Spawns Goose (``goose acp``) as a subprocess and communicates via the Agent
Client Protocol (ACP) — a JSON-RPC 2.0 protocol over newline-delimited JSON on
stdin/stdout. This is the *headless* Goose harness (``harness: goose``), the
chat-first counterpart to the terminal-first ``goose-native`` TUI harness:
output streams into the Omnigent conversation as chat, and Goose's mid-turn tool
approvals surface as web elicitation cards rather than in-terminal prompts.

Protocol flow (verified against Goose 1.38):
  1. ``initialize``   — handshake; learn ``agentCapabilities`` (prompt image
     support, ``mcpCapabilities``).
  2. ``session/new``  — create a session; Goose returns the ``sessionId`` and the
     available approval ``modes`` (auto/approve/smart_approve/chat).
  3. ``session/prompt`` — send a user turn; consume streaming ``session/update``
     notifications (``agent_message_chunk``, ``tool_call``, ``usage_update``) and
     answer any server-initiated ``session/request_permission`` requests, then
     read the final response (``stopReason`` + ``usage``).
  4. Re-use the same ``sessionId`` for subsequent turns (Goose retains context).

Goose runs its own agent loop, tool execution, context window, and compaction
internally. This executor translates the ACP event stream into Omnigent
ExecutorEvents and routes Goose's permission requests through Omnigent's
TOOL_CALL policy + human-consent elicitation (mirroring ``QwenExecutor`` /
``ClaudeSDKExecutor``).

Requirements:
    The ``goose`` CLI (v1.38+) must be installed and on PATH, configured with a
    provider (``goose configure`` → keyring / ``~/.config/goose/config.yaml``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from omnigent.inner._acp_omnigent_mcp import OmnigentAcpMcp
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    TurnComplete,
)
from omnigent.inner.os_env import OSEnvironment, create_os_environment

logger = logging.getLogger(__name__)

# ACP error code Goose maps to a filesystem "not found" (ENOENT) when a
# delegated ``fs/read_text_file`` fails — the shared ACP client lib special-
# cases exactly this code to raise an ENOENT the model understands. Any other
# code surfaces raw.
_ACP_RESOURCE_NOT_FOUND_CODE = -32002


class _AcpRequestError(Exception):
    """A handler failure to return as a JSON-RPC error on a server request.

    Carries the JSON-RPC ``code`` / ``message`` so the dispatch in
    :meth:`GooseExecutor._respond_to_agent_request` can build the error reply
    without each handler assembling the wire envelope itself.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _looks_like_missing_file(message: str) -> bool:
    """Heuristic: does an os_env error message indicate a missing path?

    The os_env helper returns failures as ``{"error": "<str>"}`` rather than
    typed exceptions, so the message text is the only signal that a read missed
    because the file is absent (vs. a permission / decode failure). Used to map
    onto the ENOENT code so the model sees "file not found".
    """
    lowered = message.lower()
    return (
        "no such file" in lowered
        or "errno 2" in lowered
        or "not found" in lowered
        or "does not exist" in lowered
    )


# ACP protocol constants (JSON-RPC 2.0 method names).
_AGENT_METHOD_INITIALIZE = "initialize"
_AGENT_METHOD_SESSION_NEW = "session/new"
_AGENT_METHOD_SESSION_PROMPT = "session/prompt"
_AGENT_METHOD_SESSION_CANCEL = "session/cancel"

# Notifications sent *from* the agent to the client.
_CLIENT_NOTIFICATION_SESSION_UPDATE = "session/update"

# Server-initiated request methods (agent → client).
_AGENT_REQUEST_REQUEST_PERMISSION = "session/request_permission"

# session/update.update.sessionUpdate values we map.
_UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
_UPDATE_TOOL_CALL = "tool_call"
_UPDATE_TOOL_CALL_UPDATE = "tool_call_update"
_UPDATE_USAGE = "usage_update"

# Idle (time-without-progress) timeouts in seconds.
_PROMPT_TIMEOUT_SECONDS = 300.0
_INIT_TIMEOUT_SECONDS = 30.0

# ACP protocol version this executor targets (Goose 1.38 → 1).
_PROTOCOL_VERSION = 1

# Default Goose builtin extensions to load over ACP. ``developer`` provides the
# core coding toolset (shell + text editor); without an extension Goose has no
# tools to act with. Overridable via ``HARNESS_GOOSE_BUILTINS`` (comma-sep).
_DEFAULT_BUILTINS = ("developer",)


def _inline_text_file_data(file_data: Any) -> str:  # type: ignore[explicit-any]
    """Decode a text ``input_file`` ``file_data`` data URI into inline text.

    Mirrors the qwen/codex executors: ``input_file`` blocks may carry a
    ``data:<mime>;base64,<payload>`` URI. Text files are decoded so the model
    sees their content; binary files (PDF, images) can't be inlined and return
    ``""``. A bare, non-data-URI string is treated as already-inline text.
    """
    if not isinstance(file_data, str) or not file_data:
        return ""
    if not file_data.startswith("data:"):
        return file_data
    try:
        import base64

        meta, b64 = file_data.split(",", 1)
        mime = meta.split(";")[0].replace("data:", "")
        if not mime.startswith("text/"):
            return ""
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — best-effort; never break a turn on a bad URI
        return ""


def _parse_image_data_uri(data_uri: Any) -> tuple[str, str] | None:  # type: ignore[explicit-any]
    """Split an ``image/*`` ``data:`` URI into ``(mime_type, base64_payload)``.

    Returns ``None`` for anything that isn't an inline ``image/*`` data URI
    (external URLs are never fetched — SSRF).
    """
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        return None
    try:
        meta, payload = data_uri.split(",", 1)
    except ValueError:
        return None
    mime = meta.split(";")[0].replace("data:", "")
    if not mime.startswith("image/") or not payload:
        return None
    return mime, payload


class GooseExecutor(Executor):
    """Executor that drives Block's Goose via its ACP (``goose acp``) mode.

    Spawns a ``goose acp`` subprocess and manages a session through the ACP
    JSON-RPC 2.0 protocol over newline-delimited stdin/stdout.
    """

    def __init__(
        self,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        provider: str | None = None,
        goose_path: str | None = None,
        builtins: Sequence[str] | None = None,
    ) -> None:
        """Initialize the Goose executor.

        :param cwd: Working directory for the goose subprocess. ``None`` inherits
            the caller's cwd.
        :param os_env: Environment / sandbox spec. When its ``sandbox`` is not
            ``"none"``, the whole ``goose`` process tree is wrapped in the
            platform sandbox (bwrap/seatbelt) at spawn — see
            :meth:`_sandbox_launch_path`.
        :param model: Optional ``GOOSE_MODEL`` override (else Goose's configured
            default). Goose has no ``session/new`` model field, so this is set in
            the subprocess env.
        :param provider: Optional ``GOOSE_PROVIDER`` override (else Goose's
            configured default).
        :param goose_path: Absolute path to the goose CLI binary. Defaults to
            ``"goose"`` (PATH lookup).
        :param builtins: Goose builtin extensions to load (``--with-builtin``).
            Defaults to :data:`_DEFAULT_BUILTINS` (``developer``).
        """
        self._cwd = cwd or os.getcwd()
        self._os_env = os_env
        # Whether to advertise ``clientCapabilities.fs`` so Goose delegates file
        # reads/writes back to us (executed through the Omnigent OSEnvironment,
        # which enforces the spec's sandbox read/write roots) instead of using
        # its own raw file tools. Enabled only when an os_env is configured and
        # it isn't a ``fork`` env — a forked env operates on a *copied* tree
        # whose path would diverge from the cwd the goose subprocess runs in.
        self._fs_delegation: bool = os_env is not None and not bool(getattr(os_env, "fork", False))
        # Live OSEnvironment backing fs delegation, created lazily on the first
        # delegated op and torn down in :meth:`close`. ``None`` until then.
        self._os_environment: OSEnvironment | None = None
        self._model = model
        self._provider = provider
        self._goose_path = goose_path or "goose"
        self._builtins = tuple(builtins) if builtins is not None else _DEFAULT_BUILTINS

        self._proc: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()  # type: ignore[explicit-any]
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._rpc_id: int = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}  # type: ignore[explicit-any]

        self._session_id: str | None = None
        self._initialized: bool = False
        self._image_supported: bool = False
        self._system_prompt_sent: bool = False

        # Context-window size (tokens) reported by Goose's ``usage_update``;
        # surfaced via :meth:`max_context_tokens` so the UI context meter fills.
        self._context_window: int | None = None

        # Bridges the ExecutorAdapter installs so Goose's mid-turn
        # ``session/request_permission`` routes through Omnigent's TOOL_CALL
        # policy + human-consent elicitation rather than blind auto-approve.
        # ``None`` means "no bridge wired" (standalone use / unit tests), in
        # which case permission falls back to allow. See :meth:`_decide_permission`.
        self._policy_evaluator: Any | None = None  # type: ignore[explicit-any]
        self._elicitation_handler: Any | None = None  # type: ignore[explicit-any]
        # Adapter-injected tool bridge + the Omnigent-tool MCP relay it backs.
        # Exposes Omnigent builtin tools to goose via session/new.mcpServers
        # (the shared serve-mcp relay); goose keeps its own developer tools.
        self._tool_executor: Any | None = None  # type: ignore[explicit-any]
        self._mcp = OmnigentAcpMcp(label="goose")
        self._omnigent_tools: list[Any] = []  # type: ignore[explicit-any]

    # ------------------------------------------------------------------
    # Low-level ACP transport
    # ------------------------------------------------------------------

    async def _start_process(self) -> None:
        """Start ``goose acp`` as an asyncio subprocess.

        The StreamReader limit is raised to 16 MiB so a large ``session/new``
        response or tool output line can't hit the default 64 KiB per-line cap.
        """
        # Reset handshake state: this may be a restart after the previous
        # subprocess died. ``_initialized`` is a one-way latch.
        self._initialized = False
        self._image_supported = False
        env = os.environ.copy()
        env.update(self._provider_env())
        argv: list[str] = ["acp"]
        for builtin in self._builtins:
            argv.extend(["--with-builtin", builtin])
        launch_path = self._sandbox_launch_path(tuple(env.keys()))
        _STREAM_LIMIT = 16 * 1024 * 1024
        self._proc = await asyncio.create_subprocess_exec(
            launch_path,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
            limit=_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    def _provider_env(self) -> dict[str, str]:
        """Build ``GOOSE_PROVIDER`` / ``GOOSE_MODEL`` overrides for the subprocess.

        Goose resolves its provider + credential from its own config
        (``goose configure`` → keyring / ``~/.config/goose/config.yaml``); these
        env vars only *override* the provider/model when the spec named one. An
        empty dict leaves Goose's ambient configuration untouched.
        """
        env: dict[str, str] = {}
        if self._provider:
            env["GOOSE_PROVIDER"] = self._provider
        if self._model:
            env["GOOSE_MODEL"] = self._model
        return env

    def _sandbox_launch_path(self, spawn_env_names: Sequence[str]) -> str:
        """Return the path to spawn — sandbox launcher or the bare goose binary.

        Mirrors :meth:`QwenExecutor._sandbox_launch_path`. When
        ``os_env.sandbox`` requests confinement, wraps the goose binary in the
        platform sandbox so the *entire* goose process tree (its builtin shell /
        editor tools) runs confined to the spec's read/write roots. Falls back to
        the bare binary (never blocks startup) when no sandbox is requested or the
        backend is unavailable.
        """
        os_env = self._os_env
        if os_env is None:
            return self._goose_path
        sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
        if sandbox_spec.type == "none":
            return self._goose_path
        try:
            from .sandbox import (
                create_exec_launcher,
                resolve_sandbox,
                with_additional_read_roots,
                with_additional_write_roots,
                with_spawn_env_allowlist,
            )

            cwd = Path(self._cwd or os.getcwd()).resolve(strict=False)
            sandbox = resolve_sandbox(os_env, cwd)
            if not sandbox.active:
                return self._goose_path
            # goose must read its own install tree and write its config/state
            # dirs (~/.config/goose, ~/.local/share/goose) and /tmp, or it can't
            # start inside the jail.
            goose_bin = Path(self._goose_path)
            if goose_bin.parent != Path("."):
                sandbox = with_additional_read_roots(sandbox, [goose_bin.resolve().parent])
            sandbox = with_additional_write_roots(
                sandbox,
                [
                    Path.home() / ".config" / "goose",
                    Path.home() / ".local" / "share" / "goose",
                    Path("/tmp"),
                ],
            )
            sandbox = with_additional_read_roots(sandbox, [Path.home() / ".config" / "goose"])
            sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
            return create_exec_launcher(self._goose_path, sandbox)
        except (OSError, ImportError, NotImplementedError) as exc:
            logger.warning("Could not apply sandbox for goose; running unsandboxed: %s", exc)
            return self._goose_path

    async def _read_stderr(self) -> None:
        """Continuously drain goose stderr, logging each line at debug.

        Prevents a chatty CLI from filling the OS pipe buffer (~64 KiB) and
        stalling the turn.
        """
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw_line = await self._proc.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.debug("goose stderr: %s", line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("goose stderr reader stopped: %s", exc)

    async def _read_stdout(self) -> None:
        """Continuously read NDJSON lines from goose stdout.

        Responses (``id`` + no ``method``) resolve the matching ``_pending``
        future; notifications and server-initiated requests go on ``_queue`` for
        ``run_turn`` to consume.
        """
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw_line = await self._proc.stdout.readline()
                if not raw_line:
                    # EOF — the goose subprocess exited. Wake in-flight futures so
                    # run_turn fails fast instead of blocking until idle timeout.
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(EOFError("goose subprocess closed stdout"))
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(line)  # type: ignore[explicit-any]
                except json.JSONDecodeError:
                    logger.debug("goose: non-JSON stdout line: %r", line[:200])
                    continue

                msg_id = msg.get("id")
                # Match a response by "id + no method": goose's own requests
                # (session/request_permission) also carry an id, so the method
                # check prevents a colliding request from mis-resolving our future.
                if msg_id is not None and "method" not in msg and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    await self._queue.put(msg)
        except (asyncio.CancelledError, EOFError):
            pass
        except Exception as exc:
            logger.exception("goose stdout reader error: %s", exc)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            await self._queue.put({"type": "error", "message": str(exc)})

    async def _send(self, msg: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Write one newline-terminated JSON message to goose stdin."""
        assert self._proc and self._proc.stdin
        encoded = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(encoded)
        await self._proc.stdin.drain()

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],  # type: ignore[explicit-any]
        timeout: float = _INIT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Send a JSON-RPC 2.0 request and await its response."""
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()  # type: ignore[explicit-any]
        self._pending[req_id] = fut

        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    # ------------------------------------------------------------------
    # ACP handshake
    # ------------------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        """Perform the ``initialize`` handshake if not already done."""
        if self._initialized:
            return
        resp = await self._rpc(
            _AGENT_METHOD_INITIALIZE,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "clientInfo": {"name": "omnigent", "version": "1.0"},
                # Advertise fs delegation so Goose routes file reads/writes back
                # to us (executed via the OSEnvironment) when an os_env is
                # configured; both false (no os_env / fork env) leaves Goose on
                # its own builtin tools. Terminal stays unadvertised.
                "clientCapabilities": {
                    "fs": {
                        "readTextFile": self._fs_delegation,
                        "writeTextFile": self._fs_delegation,
                    },
                    "terminal": False,
                },
            },
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        if "error" in resp:
            raise RuntimeError(
                f"goose ACP initialize failed: {resp['error'].get('message', resp['error'])}"
            )
        prompt_caps = (
            (resp.get("result") or {}).get("agentCapabilities", {}).get("promptCapabilities", {})
        )
        self._image_supported = bool(prompt_caps.get("image"))
        self._initialized = True

    async def _ensure_session(self) -> str:
        """Create (or reuse) an ACP session, returning Goose's assigned id.

        Goose assigns its own ``sessionId`` (a date-stamped id like
        ``20260623_1``); we send only ``cwd`` + ``mcpServers`` and use whatever
        the server returns.
        """
        if self._session_id is not None:
            return self._session_id

        mcp_servers = self._mcp.session_new_servers(
            tools=self._omnigent_tools,
            tool_executor=getattr(self, "_tool_executor", None),
            loop=asyncio.get_event_loop(),
        )
        resp = await self._rpc(
            _AGENT_METHOD_SESSION_NEW,
            {"cwd": self._cwd, "mcpServers": mcp_servers},
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        if "error" in resp:
            raise RuntimeError(
                f"goose ACP session/new failed: {resp['error'].get('message', resp['error'])}"
            )
        result = resp.get("result", {})
        server_session_id = result.get("sessionId")
        if not server_session_id:
            raise RuntimeError(
                "goose ACP session/new response missing sessionId: " + json.dumps(resp)[:200]
            )
        self._session_id = server_session_id
        return self._session_id

    # ------------------------------------------------------------------
    # Server-initiated requests (agent → client)
    # ------------------------------------------------------------------

    async def _respond_to_agent_request(self, request: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Answer a server-initiated ACP request from goose.

        - ``session/request_permission`` — decide via Omnigent's TOOL_CALL policy
          + human-consent elicitation (:meth:`_decide_permission`), then select
          the matching allow/reject option. NOT a blind approve.
        - ``fs/read_text_file`` / ``fs/write_text_file`` — when fs delegation is
          advertised (an os_env is configured; see :attr:`_fs_delegation`), Goose
          routes its file I/O here, executed through the Omnigent OSEnvironment so
          the spec's sandbox read/write roots are enforced. Off → never arrive.
        - anything else — reply with JSON-RPC ``method not found`` so goose fails
          loudly rather than acting on empty data.
        """
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        logger.debug("goose agent request: method=%s id=%s", method, req_id)

        result: dict[str, Any] | None = None  # type: ignore[explicit-any]
        error: dict[str, Any] | None = None  # type: ignore[explicit-any]
        try:
            if method == _AGENT_REQUEST_REQUEST_PERMISSION:
                allow = await self._decide_permission(params)
                result = {"outcome": self._permission_outcome(params, allow=allow)}
            elif method == "fs/read_text_file" and self._fs_delegation:
                result = await self._handle_fs_read(params)
            elif method == "fs/write_text_file" and self._fs_delegation:
                result = await self._handle_fs_write(params)
            else:
                error = {
                    "code": -32601,
                    "message": f"omnigent: unsupported ACP request method {method!r}",
                }
        except _AcpRequestError as exc:
            error = {"code": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001
            logger.debug("goose agent request %s failed: %s", method, exc)
            error = {"code": -32603, "message": f"{method} failed: {exc}"}

        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}  # type: ignore[explicit-any]
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        await self._send(reply)

    # ------------------------------------------------------------------
    # Filesystem delegation (goose → client, when fs capability advertised)
    # ------------------------------------------------------------------

    async def _ensure_os_environment(self) -> OSEnvironment:
        """Lazily create the OSEnvironment backing fs delegation.

        :returns: The live OSEnvironment for this executor's os_env spec.
        :raises _AcpRequestError: When no usable os_env can be created.
        """
        if self._os_environment is None:
            env = create_os_environment(self._os_env)
            if env is None:
                raise _AcpRequestError(-32603, "omnigent: no os_env for fs delegation")
            self._os_environment = env
        return self._os_environment

    async def _handle_fs_read(self, params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Serve an ACP ``fs/read_text_file`` by reading through the OSEnvironment.

        ACP params ``{path, line?, limit?}`` (1-based start line, max line count;
        both optional → whole file) map onto :meth:`OSEnvironment.read`.

        :param params: The request params.
        :returns: ``{"content": <text>}`` per the ACP response shape.
        :raises _AcpRequestError: On a missing path arg, a non-text/binary file,
            or a read failure (mapped to ENOENT when it looks like a missing
            file so goose raises the right error to the model).
        """
        path = params.get("path")
        if not isinstance(path, str) or not path:
            raise _AcpRequestError(-32602, "fs/read_text_file requires a string 'path'")
        line = params.get("line")
        limit = params.get("limit")
        offset = line if isinstance(line, int) and line >= 1 else 1
        read_limit = limit if isinstance(limit, int) and limit >= 1 else None

        env = await self._ensure_os_environment()
        result = await env.read(path, offset=offset, limit=read_limit)
        if "error" in result:
            message = str(result["error"])
            code = _ACP_RESOURCE_NOT_FOUND_CODE if _looks_like_missing_file(message) else -32603
            raise _AcpRequestError(code, message)
        if result.get("encoding") != "utf-8":
            raise _AcpRequestError(-32603, f"{path}: not a UTF-8 text file")
        return {"content": result.get("content", "")}

    async def _handle_fs_write(self, params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Serve an ACP ``fs/write_text_file`` by writing through the OSEnvironment.

        ACP params ``{path, content}``; the write goes through the helper so the
        spec's sandbox write roots are enforced at the Python layer.

        :param params: The request params.
        :returns: An empty result object (ACP expects no payload on success).
        :raises _AcpRequestError: On missing/invalid args or a write failure.
        """
        path = params.get("path")
        content = params.get("content")
        if not isinstance(path, str) or not path:
            raise _AcpRequestError(-32602, "fs/write_text_file requires a string 'path'")
        if not isinstance(content, str):
            raise _AcpRequestError(-32602, "fs/write_text_file requires string 'content'")

        env = await self._ensure_os_environment()
        result = await env.write(path, content)
        if "error" in result:
            raise _AcpRequestError(-32603, str(result["error"]))
        return {}

    @staticmethod
    def _extract_tool_call(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:  # type: ignore[explicit-any]
        """Pull ``(tool_name, tool_input)`` from a ``session/request_permission``.

        Goose's payload carries a ``toolCall`` with a human ``title`` (e.g.
        ``"shell"``), a ``kind`` (e.g. ``"other"``), a ``rawInput`` dict (e.g.
        ``{"command": "rm …"}``), and — on the streamed ``tool_call`` update —
        ``_meta.goose.toolCall.toolName``. We prefer the precise tool name when
        present, else the title, else the kind.
        """
        tool_call = params.get("toolCall") or {}
        meta_goose = (tool_call.get("_meta") or {}).get("goose") or {}
        inner = meta_goose.get("toolCall") or {}
        name = inner.get("toolName") or tool_call.get("title") or tool_call.get("kind") or "tool"
        args = tool_call.get("rawInput")
        if not isinstance(args, dict):
            args = {}
        return str(name), args

    async def _decide_permission(self, params: dict[str, Any]) -> bool:  # type: ignore[explicit-any]
        """Decide allow/deny for a permission request — policy then elicitation.

        Mirrors :meth:`QwenExecutor._decide_permission`:

        1. **TOOL_CALL policy** (:attr:`_policy_evaluator`): a hard
           ``POLICY_ACTION_DENY`` denies; ``POLICY_ACTION_ASK`` defers to
           elicitation (and **fails closed** when no handler is wired);
           ``ALLOW`` / unspecified falls through.
        2. **Human-consent elicitation** (:attr:`_elicitation_handler`): routes
           to the user via ``ctx.elicit`` (a web approval card) and returns their
           accept/deny.

        When neither bridge is wired (standalone / unit tests), falls back to
        allow so direct use of the executor isn't blocked. In normal runner
        operation the adapter installs both, so destructive actions are gated.
        """
        tool_name, tool_input = self._extract_tool_call(params)
        handler = getattr(self, "_elicitation_handler", None)
        policy_eval = getattr(self, "_policy_evaluator", None)

        if policy_eval is not None:
            action: str | None
            try:
                verdict = await policy_eval(
                    "PHASE_TOOL_CALL", {"name": tool_name, "arguments": tool_input}
                )
                action = getattr(verdict, "action", None)
            except Exception as exc:  # noqa: BLE001 — fail open to elicitation
                logger.warning("goose TOOL_CALL policy eval failed for %s: %s", tool_name, exc)
                action = None
            if action == "POLICY_ACTION_DENY":
                logger.info("goose permission denied by policy: tool=%s", tool_name)
                return False
            if action == "POLICY_ACTION_ASK":
                if handler is None:
                    logger.warning(
                        "goose TOOL_CALL policy ASK with no elicitation handler; denying tool=%s",
                        tool_name,
                    )
                    return False
                allowed = bool(await handler(tool_name, tool_input))
                logger.info(
                    "goose permission %s by user (policy ASK): tool=%s",
                    "allowed" if allowed else "denied",
                    tool_name,
                )
                return allowed
            # ALLOW / UNSPECIFIED / unknown → fall through to elicitation.

        if handler is not None:
            allowed = bool(await handler(tool_name, tool_input))
            logger.info(
                "goose permission %s by user: tool=%s",
                "allowed" if allowed else "denied",
                tool_name,
            )
            return allowed

        logger.debug("goose permission allowed (no policy/elicitation wired): tool=%s", tool_name)
        return True

    @staticmethod
    def _permission_outcome(  # type: ignore[explicit-any]
        params: dict[str, Any], *, allow: bool
    ) -> dict[str, Any]:
        """Map an allow/deny decision to an ACP permission ``outcome``.

        On allow, prefer a once-scoped grant (``allow_once``) over
        ``allow_always`` so we never persist a blanket "always allow". On deny,
        pick a ``reject_*`` option, or ``cancelled`` when none is offered. Goose's
        options carry both ``optionId`` and ``kind`` set to e.g. ``allow_once``.
        """
        options = [o for o in (params.get("options") or []) if isinstance(o, dict)]

        def _pick(*kinds: str) -> dict[str, Any] | None:  # type: ignore[explicit-any]
            for kind in kinds:
                for opt in options:
                    if opt.get("kind") == kind:
                        return opt
            return None

        if allow:
            chosen = _pick("allow_once", "allow_always") or next(
                (o for o in options if "allow" in str(o.get("kind", ""))), None
            )
            if chosen is None:
                return {"outcome": "cancelled"}
        else:
            chosen = _pick("reject_once", "reject_always") or next(
                (o for o in options if "reject" in str(o.get("kind", ""))), None
            )
            if chosen is None:
                return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": chosen.get("optionId")}

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _image_blocks_from_content(content: Any) -> list[dict[str, Any]]:  # type: ignore[explicit-any]
        """Build ACP ``image`` prompt blocks from a message's ``input_image`` blocks."""
        out: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        if not isinstance(content, list):
            return out
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "input_image":
                continue
            parsed = _parse_image_data_uri(block.get("image_url") or block.get("file_data"))
            if parsed:
                mime, data = parsed
                out.append({"type": "image", "mimeType": mime, "data": data})
        return out

    @staticmethod
    def _text_from_blocks(
        blocks: list[Any],
        *,
        emit_image_marker: bool = False,  # type: ignore[explicit-any]
    ) -> str:
        """Extract prompt text from a Responses-API content-block list.

        ACP's ``session/prompt`` text part is plain text, so each block is folded:
        ``input_text``/``output_text``/``text`` verbatim; ``input_file`` inlined
        (fenced) when the runner resolved it to a text data URI, else a marker;
        ``input_image`` as a marker only when *emit_image_marker* is set (the
        image otherwise goes as a real ACP image block).
        """
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("input_text", "output_text", "text"):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif btype == "input_file":
                name = block.get("filename") or block.get("file_id") or "file"
                inlined = _inline_text_file_data(block.get("file_data"))
                if inlined:
                    parts.append(
                        f"--- attached file: {name} ---\n{inlined}\n--- end of {name} ---"
                    )
                else:
                    parts.append(f"[attached file: {name}]")
            elif btype == "input_image" and emit_image_marker:
                name = block.get("filename") or block.get("file_id")
                parts.append(f"[attached image: {name}]" if name else "[attached image]")
        return "\n".join(parts)

    @classmethod
    def _history_prefix(cls, prior: list[Any]) -> str:  # type: ignore[explicit-any]
        """Serialize prior conversation turns into a text prefix.

        On a *fresh* ACP session (the first turn of a newly spawned/respawned
        ``goose acp`` process, or after a session reset) Goose holds none of the
        earlier conversation — its context lived in the dead subprocess. Since
        :meth:`run_turn` normally sends only the latest user turn (relying on
        the persistent session to retain history), we'd lose everything before
        the switch. Replaying the transcript as a labeled ``role: content``
        block restores that context, mirroring
        ``ClaudeSDKExecutor._build_prompt``. A ``/model`` switch respawns the
        subprocess (see HarnessProcessManager), so this is what keeps a
        mid-conversation model change from dropping the thread.

        :param prior: The conversation turns *before* the latest user message
            (each an inner ``Message`` dict).
        :returns: A ``"Conversation so far: …"`` text block, or ``""`` when
            there is nothing to replay.
        """
        lines = ["Conversation so far:"]
        for msg in prior:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "user")).replace("_", " ")
            raw = msg.get("content")
            if raw is None:
                content = ""
            elif isinstance(raw, str):
                content = raw
            elif isinstance(raw, list):
                content = cls._text_from_blocks(raw, emit_image_marker=True)
            else:
                content = json.dumps(raw, ensure_ascii=True)
            lines.append(f"{role}: {content}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Executor interface
    # ------------------------------------------------------------------

    def max_context_tokens(self) -> int | None:
        """Return Goose's reported context-window size, if observed yet.

        Goose streams ``usage_update {used, size}`` where ``size`` is the model's
        context window; surfacing it fills the UI's context meter. ``None`` until
        the first ``usage_update`` of the session arrives.
        """
        return self._context_window

    @staticmethod
    def _usage_from_result(result: dict[str, Any]) -> dict[str, Any] | None:  # type: ignore[explicit-any]
        """Map Goose's final ``result.usage`` to Omnigent's usage keys.

        Goose reports ``{totalTokens, inputTokens, outputTokens}``; Omnigent's
        ``TurnComplete.usage`` uses ``{input_tokens, output_tokens, total_tokens}``.
        """
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        out: dict[str, Any] = {}
        if isinstance(usage.get("inputTokens"), int):
            out["input_tokens"] = usage["inputTokens"]
        if isinstance(usage.get("outputTokens"), int):
            out["output_tokens"] = usage["outputTokens"]
        if isinstance(usage.get("totalTokens"), int):
            out["total_tokens"] = usage["totalTokens"]
        return out or None

    async def interrupt_session(self, session_key: str) -> bool:  # noqa: ARG002
        """Interrupt a running Goose turn, making the web Stop button functional.

        Sends the ACP ``session/cancel`` notification to request a clean stop —
        Goose ends the in-flight ``session/prompt`` with a ``cancelled`` stop
        reason, which the ``run_turn`` loop then surfaces as a partial result.
        ``session/cancel`` is a notification (no ``id``, no reply), so it's sent
        via ``_send`` rather than ``_rpc``. If no session has been established
        yet (still in the initialize/session-new handshake) or the send fails,
        falls back to SIGTERM on the subprocess so the turn always terminates.

        Returns True when any interrupt action was taken (cancel sent or process
        signalled), False when there is no live process to interrupt.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False

        session_id = self._session_id
        if session_id is not None:
            # Preferred path: ask Goose to cancel cleanly over ACP. The agent
            # doesn't reply to the notification; it ends the running prompt with
            # a cancelled stop reason, which run_turn observes.
            try:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": _AGENT_METHOD_SESSION_CANCEL,
                        "params": {"sessionId": session_id},
                    }
                )
                logger.info("goose interrupt: sent session/cancel for session=%s", session_id)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "goose interrupt: session/cancel failed for session=%s (%s); "
                    "falling back to SIGTERM",
                    session_id,
                    exc,
                )

        # Fallback: SIGTERM the subprocess (mirrors KimiExecutor).
        return self._interrupt_proc()

    def _interrupt_proc(self) -> bool:
        """Send SIGTERM to the goose subprocess, returning True if signalled.

        Safe to call at any time — no-ops when the process has already exited.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
            return True
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[Any],  # type: ignore[explicit-any]  # goose runs its own tools; used for the Omnigent MCP relay
        system_prompt: str,
        config: ExecutorConfig | None = None,  # noqa: ARG002 — unused; required by the interface
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn of the Goose agent loop via ACP.

        Sends ``session/prompt`` and yields ``TextChunk`` events as the agent
        streams, answering any ``session/request_permission`` mid-turn, until the
        final response (``stopReason``) arrives — then yields ``TurnComplete``
        with token usage.
        """
        # Captured for the Omnigent MCP relay set up lazily at session/new.
        self._omnigent_tools = tools or []
        try:
            if self._proc is None or self._proc.returncode is not None:
                await self._start_process()
            await self._ensure_initialized()
            session_id = await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            yield ExecutorError(message=str(exc), retryable=False)
            return

        # A fresh ACP session (first turn of a new/respawned process, or after
        # a reset) holds no prior context. Captured before we flip the latch
        # below so we know whether to replay history into this turn.
        fresh_session = not self._system_prompt_sent

        # Build the prompt payload from the most recent user message.
        user_text = ""
        image_blocks: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        latest_user_idx: int | None = None
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            if role == "user":
                latest_user_idx = idx
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if isinstance(content, str):
                    user_text = content
                elif isinstance(content, list):
                    if self._image_supported:
                        image_blocks = self._image_blocks_from_content(content)
                    user_text = self._text_from_blocks(
                        content, emit_image_marker=not self._image_supported
                    )
                break

        # On a fresh session, replay the prior conversation so a model switch
        # (which respawns the subprocess) or a session reset doesn't drop the
        # thread — Goose otherwise only ever sees this turn's latest message.
        # Skipped when there's nothing before the latest user turn (the genuine
        # first turn of a brand-new conversation). See :meth:`_history_prefix`.
        if fresh_session and latest_user_idx is not None and latest_user_idx > 0:
            history_prefix = self._history_prefix(messages[:latest_user_idx])
            user_text = f"{history_prefix}\n\nuser: {user_text}" if user_text else history_prefix

        # ACP has no system-prompt field, so fold it into the first turn. The
        # latch flips on any fresh session — even with an empty system prompt —
        # so a continuing session never re-replays history or re-folds.
        if fresh_session:
            if system_prompt:
                user_text = f"{system_prompt}\n\n{user_text}" if user_text else system_prompt
            self._system_prompt_sent = True

        prompt_blocks: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        if user_text or not image_blocks:
            prompt_blocks.append({"type": "text", "text": user_text})
        prompt_blocks.extend(image_blocks)

        # Drain stale items from a prior turn; answer any leftover server request.
        while not self._queue.empty():
            try:
                stale = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(stale, dict) and stale.get("id") is not None and stale.get("method"):
                await self._respond_to_agent_request(stale)

        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()  # type: ignore[explicit-any]
        self._pending[req_id] = fut

        await self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": _AGENT_METHOD_SESSION_PROMPT,
                "params": {"sessionId": session_id, "prompt": prompt_blocks},
            }
        )

        deadline = loop.time() + _PROMPT_TIMEOUT_SECONDS
        accumulated_text: list[str] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                yield ExecutorError(message="Timeout waiting for goose response", retryable=True)
                return

            # Complete only once the future is resolved AND the queue is drained,
            # so trailing chunks aren't truncated.
            if fut.done() and self._queue.empty():
                try:
                    response = fut.result()
                except Exception as exc:  # noqa: BLE001
                    self._session_id = None
                    self._system_prompt_sent = False
                    yield ExecutorError(message=f"goose process error: {exc}", retryable=True)
                    return
                if "error" in response:
                    error_msg = response["error"].get("message", "Unknown ACP error")
                    if "Session not found" in error_msg:
                        self._session_id = None
                        self._system_prompt_sent = False
                    yield ExecutorError(message=error_msg, retryable=True)
                    return
                result = response.get("result", {}) if isinstance(response, dict) else {}
                usage = self._usage_from_result(result) if isinstance(result, dict) else None
                yield TurnComplete(response="".join(accumulated_text), usage=usage)
                return

            try:
                notification = await asyncio.wait_for(
                    self._queue.get(), timeout=min(remaining, 2.0)
                )
            except asyncio.TimeoutError:
                continue

            method = notification.get("method", "")
            params = notification.get("params", {})

            if method == _CLIENT_NOTIFICATION_SESSION_UPDATE:
                update = params.get("update", {})
                update_type = update.get("sessionUpdate", "")

                if update_type == _UPDATE_AGENT_MESSAGE_CHUNK:
                    content = update.get("content", {})
                    text = content.get("text", "") if isinstance(content, dict) else ""
                    if text:
                        accumulated_text.append(text)
                        yield TextChunk(text=text)
                elif update_type == _UPDATE_USAGE:
                    size = update.get("size")
                    if isinstance(size, int) and size > 0:
                        self._context_window = size
                elif update_type == _UPDATE_TOOL_CALL:
                    logger.debug("goose tool_call: %s", update.get("title", "tool_call"))
                elif update_type == _UPDATE_TOOL_CALL_UPDATE:
                    pass

            elif notification.get("id") is not None and notification.get("method"):
                # Server-initiated request (session/request_permission): routes
                # through policy + elicitation. Blocks while the human decides.
                await self._respond_to_agent_request(notification)

            # Inbound message = progress; reset the idle deadline (after the
            # approval block so a slow approval doesn't time out).
            deadline = loop.time() + _PROMPT_TIMEOUT_SECONDS

    async def close_session(self, session_key: str) -> None:
        """Close a named session (no-op; the ACP session is per-process)."""

    async def close(self) -> None:
        """Terminate the goose subprocess and clean up."""
        with contextlib.suppress(Exception):
            self._mcp.close()
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        # Release the fs-delegation OSEnvironment's helper subprocess, if one
        # was spawned for a delegated file op this session.
        if self._os_environment is not None:
            with contextlib.suppress(Exception):
                self._os_environment.close()
            self._os_environment = None
        if self._proc:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()  # type: ignore[union-attr]
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    self._proc.kill()
            finally:
                self._proc = None
