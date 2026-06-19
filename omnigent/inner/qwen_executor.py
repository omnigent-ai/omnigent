"""QwenExecutor: run agents through Qwen Code's ACP mode.

Spawns Qwen (``qwen --acp``) as a subprocess and communicates via the
Agent Communication Protocol (ACP) — a JSON-RPC 2.0 protocol over
newline-delimited JSON on stdin/stdout.

Protocol flow:
  1. ``initialize``  — handshake, learn capabilities.
  2. ``session/new`` — create a session, get back the server-assigned sessionId.
  3. ``session/prompt`` — send a user turn; wait for streaming
     ``session/update`` notifications and the final response.
  4. Repeat step 3 for subsequent turns (``session/load`` or just re-use the
     same sessionId if the server keeps it alive across prompts).

Qwen manages its own agent loop, tool execution, context window, and
compaction internally.  This executor translates the ACP event stream into
Omnigent ExecutorEvents.

Requirements:
    The ``qwen`` CLI (v0.18+) must be installed and on PATH.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    TurnComplete,
)

logger = logging.getLogger(__name__)

# ACP protocol constants (JSON-RPC 2.0 method names)
_AGENT_METHOD_INITIALIZE = "initialize"
_AGENT_METHOD_SESSION_NEW = "session/new"
_AGENT_METHOD_SESSION_LOAD = "session/load"
_AGENT_METHOD_SESSION_PROMPT = "session/prompt"
_AGENT_METHOD_SESSION_CANCEL = "session/cancel"

# Notifications sent *from* the agent to the client
_CLIENT_NOTIFICATION_SESSION_UPDATE = "session/update"

# session/update.update.sessionUpdate values we care about
_UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
_UPDATE_TOOL_CALL = "tool_call"
_UPDATE_TOOL_CALL_UPDATE = "tool_call_update"

# How long (seconds) to wait for qwen to respond to a JSON-RPC request
# before treating the turn as timed out.
_PROMPT_TIMEOUT_SECONDS = 300.0
_INIT_TIMEOUT_SECONDS = 30.0

# ACP protocol version this executor targets.
_PROTOCOL_VERSION = 1


class QwenExecutor(Executor):
    """Executor that drives Qwen Code via its ACP (``--acp``) mode.

    Spawns a ``qwen --acp`` subprocess and manages sessions through the
    ACP JSON-RPC 2.0 protocol over newline-delimited stdin/stdout.
    """

    def __init__(
        self,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        qwen_path: str | None = None,
    ) -> None:
        """Initialize the Qwen executor.

        :param cwd: Working directory for the qwen subprocess.  When
            ``None``, the subprocess inherits the caller's cwd.
        :param os_env: Environment spec for sandboxing (currently unused
            by this executor but accepted for API parity).
        :param model: Model identifier to pass in ``session/new``.
        :param qwen_path: Absolute path to qwen CLI binary.
            Defaults to ``"qwen"`` (PATH lookup).
        """
        self._cwd = cwd or os.getcwd()
        self._os_env = os_env
        self._model = model
        self._qwen_path = qwen_path or "qwen"

        # Asyncio subprocess (created on first run_turn call).
        self._proc: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]

        # Queue fed by the stdout-reader coroutine.
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()  # type: ignore[explicit-any]
        self._reader_task: asyncio.Task[None] | None = None

        # Monotonically increasing JSON-RPC request id.
        self._rpc_id: int = 0

        # Pending RPC responses keyed by request id.
        # When _reader_task receives a response (has "id" + "result"/"error"),
        # it places it here for the awaiting coroutine.
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}  # type: ignore[explicit-any]

        # ACP session id assigned by qwen (returned in session/new response).
        self._session_id: str | None = None

        # Whether initialize has been sent already.
        self._initialized: bool = False

    # ------------------------------------------------------------------
    # Low-level ACP helpers
    # ------------------------------------------------------------------

    async def _start_process(self) -> None:
        """Start ``qwen --acp`` as an asyncio subprocess.

        The StreamReader limit is set to 16 MiB so that qwen's large
        ``session/new`` responses (which can list dozens of available
        models) don't hit the default 64 KiB per-line cap and raise
        "Separator is not found, and chunk exceed the limit".
        """
        env = os.environ.copy()
        # 16 MiB per-line limit for the stdout StreamReader.
        _STREAM_LIMIT = 16 * 1024 * 1024
        self._proc = await asyncio.create_subprocess_exec(
            self._qwen_path,
            "--acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
            limit=_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())

    async def _read_stdout(self) -> None:
        """Continuously read NDJSON lines from qwen stdout.

        Decoded messages are dispatched:
        - Responses (have ``"id"`` key + ``"result"``/``"error"``) are
          resolved into the matching ``_pending`` future.
        - Notifications (have ``"method"`` key, no ``"id"``) are put on
          ``_queue`` for ``run_turn`` to consume.

        Uses ``readline()`` directly instead of ``async for line in
        stdout`` to benefit from the raised StreamReader limit set at
        process creation time (the iteration protocol falls back to the
        chunk limit rather than the configured per-line limit in some
        Python versions).
        """
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw_line = await self._proc.stdout.readline()
                if not raw_line:
                    break  # EOF — process exited
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(line)  # type: ignore[explicit-any]
                except json.JSONDecodeError:
                    logger.debug("qwen: non-JSON stdout line: %r", line[:200])
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    # Response to one of our requests.
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    # Notification or unknown — forward to run_turn queue.
                    await self._queue.put(msg)
        except (asyncio.CancelledError, EOFError):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("qwen stdout reader error: %s", exc)
            # Wake any pending futures with an error so callers don't block
            # forever when the process dies unexpectedly.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            await self._queue.put({"type": "error", "message": str(exc)})

    async def _send(self, msg: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Write one newline-terminated JSON message to qwen stdin."""
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
        """Send a JSON-RPC 2.0 request and await its response.

        :param method: RPC method name, e.g. ``"initialize"``.
        :param params: Request parameters.
        :param timeout: Maximum seconds to wait for the response.
        :returns: The full response message dict (containing ``"result"`` or
            ``"error"``).
        :raises asyncio.TimeoutError: If no response arrives within *timeout*.
        :raises RuntimeError: If the process is not running.
        """
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()  # type: ignore[explicit-any]
        self._pending[req_id] = fut

        request: dict[str, Any] = {  # type: ignore[explicit-any]
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._send(request)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def _notify(self, method: str, params: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Send a JSON-RPC 2.0 notification (no response expected)."""
        notification: dict[str, Any] = {  # type: ignore[explicit-any]
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._send(notification)

    # ------------------------------------------------------------------
    # ACP handshake helpers
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
            },
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        if "error" in resp:
            raise RuntimeError(
                f"qwen ACP initialize failed: {resp['error'].get('message', resp['error'])}"
            )
        self._initialized = True

    async def _ensure_session(self) -> str:
        """Create (or reuse) an ACP session, returning its server-assigned id.

        :returns: The session id string assigned by qwen.
        """
        if self._session_id is not None:
            return self._session_id

        params: dict[str, Any] = {  # type: ignore[explicit-any]
            "sessionId": secrets.token_urlsafe(16),
            "cwd": self._cwd,
            "mcpServers": [],
        }
        if self._model:
            params["model"] = self._model

        resp = await self._rpc(
            _AGENT_METHOD_SESSION_NEW,
            params,
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        if "error" in resp:
            raise RuntimeError(
                f"qwen ACP session/new failed: {resp['error'].get('message', resp['error'])}"
            )

        # Qwen assigns (possibly remaps) the session id — always use what
        # the server returns, not what we sent.
        result = resp.get("result", {})
        server_session_id = result.get("sessionId")
        if not server_session_id:
            raise RuntimeError(
                "qwen ACP session/new response missing sessionId: "
                + json.dumps(resp)[:200]
            )
        self._session_id = server_session_id
        return self._session_id

    # ------------------------------------------------------------------
    # Executor interface
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[Any],  # type: ignore[explicit-any]
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn of the Qwen agent loop via ACP.

        Sends a ``session/prompt`` request and yields events until the
        turn completes (``stopReason`` present in the response) or an
        error occurs.

        :param messages: Conversation history.
        :param tools: Tool specs (not passed directly to Qwen; Qwen uses
            its own tool registry — MCP bridging is TODO).
        :param system_prompt: Instructions for the session.
        :param config: Optional executor config (model override etc.).
        """
        # Lazily boot the subprocess.
        if self._proc is None or self._proc.returncode is not None:
            await self._start_process()

        try:
            await self._ensure_initialized()
            session_id = await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            yield ExecutorError(message=str(exc), retryable=False)
            return

        # Build the prompt payload from the most recent user message.
        user_text = ""
        for msg in reversed(messages):
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            if role == "user":
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if isinstance(content, str):
                    user_text = content
                elif isinstance(content, list):
                    parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    user_text = "\n".join(parts)
                break

        prompt_blocks = [{"type": "text", "text": user_text}]

        # Drain any stale notifications from a prior turn.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Send the turn — this is a JSON-RPC *request*, so we wait for
        # both streaming notifications AND the final response.
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()  # type: ignore[explicit-any]
        self._pending[req_id] = fut

        prompt_request: dict[str, Any] = {  # type: ignore[explicit-any]
            "jsonrpc": "2.0",
            "id": req_id,
            "method": _AGENT_METHOD_SESSION_PROMPT,
            "params": {
                "sessionId": session_id,
                "prompt": prompt_blocks,
            },
        }
        await self._send(prompt_request)

        # Yield events until qwen signals turn completion.
        deadline = loop.time() + _PROMPT_TIMEOUT_SECONDS
        accumulated_text: list[str] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                yield ExecutorError(
                    message="Timeout waiting for qwen response", retryable=True
                )
                return

            # Check if the final response arrived.
            if fut.done():
                response = fut.result()
                if "error" in response:
                    error_msg = response["error"].get("message", "Unknown ACP error")
                    # If the session was lost, reset so next turn creates a new one.
                    if "Session not found" in error_msg:
                        self._session_id = None
                    yield ExecutorError(message=error_msg, retryable=True)
                    return
                # Successful completion.
                final_text = "".join(accumulated_text)
                if final_text:
                    yield TurnComplete(response=final_text)
                else:
                    yield TurnComplete(response="")
                return

            # Otherwise consume queued notifications.
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
                    if isinstance(content, dict):
                        text = content.get("text", "")
                    else:
                        text = ""
                    if text:
                        accumulated_text.append(text)
                        yield TextChunk(text=text)

                elif update_type == _UPDATE_TOOL_CALL:
                    # Qwen is executing a built-in tool — surface it as info.
                    tool_title = update.get("title", "tool_call")
                    logger.debug("qwen tool_call: %s", tool_title)

                elif update_type == _UPDATE_TOOL_CALL_UPDATE:
                    # Status update on an in-progress tool call — skip.
                    pass

            elif notification.get("id") is not None:
                # An incoming *request* from qwen (e.g. session/request_permission,
                # fs/read_text_file, terminal/*).  For now, auto-approve everything
                # and return an empty success response so qwen doesn't hang.
                req_id_from_agent = notification["id"]
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id_from_agent,
                        "result": {},
                    }
                )

    async def close_session(self, session_key: str) -> None:
        """Close a named session (no-op; sessions are per-process)."""

    async def close(self) -> None:
        """Terminate the qwen subprocess and clean up."""
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

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
