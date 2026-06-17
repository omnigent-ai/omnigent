"""QwenExecutor: run agents through Qwen Code's RPC mode.

Spawns Qwen (``qwen --mode rpc``) as a subprocess and communicates via a JSONL
protocol over stdin/stdout. Qwen manages its own agent loop, tool execution,
context window, and compaction internally. This executor translates the Qwen
event stream into Omnigent ExecutorEvents.

Omnigent tools are bridged into Qwen via MCP tool registration. Tool execution
is proxied over a local TCP socket to the Omnigent Python process, so
policies, history recording, sub-agents, runtime, and all other Omnigent
features work exactly as they do with other harnesses.

Requirements:
    The ``qwen`` CLI must be installed and on PATH.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeAlias

# Import runtime types at module level for use in class definitions.
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)

# Type aliases for JSON-shaped boundaries
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Tool executor callable wired in by omnigent.Session.
# Note: This is defined here since ToolExecutor doesn't exist in executor.py
_ToolExecutor: TypeAlias = Callable[  # type: ignore[explicit-any]
    [str, dict[str, Any]], Awaitable[dict[str, Any]]
]


class _ToolServer:
    """TCP server that handles tool-call requests from Qwen.

    Protocol (JSONL over TCP):
        Request:  {"id":"...","token":"...","tool":"tool_name","args":{...}}
        Response: {"id":"...","result":{...}} or {"id":"...","error":"..."}

    The loopback socket is reachable by any co-located process, so every
    request must carry a token (a per-server secret embedded in the MCP config).
    """

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self.port: int = 0
        self._tool_executor: _ToolExecutor | None = None
        # Per-server bearer token required on every request.
        self.token: str = secrets.token_urlsafe(32)

    async def start(self) -> int:
        """Start the TCP server and return the bound port."""
        self._server = await asyncio.start_server(
            self._handle_client,
            "127.0.0.1",
            0,  # Let OS pick an ephemeral port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming tool-call requests."""
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                # Validate token
                req_token = request.get("token")
                if req_token != self.token:
                    logger.warning("Invalid tool server token")
                    continue

                req_id = request.get("id")
                tool_name = request.get("tool")
                args = request.get("args", {})

                result: dict[str, Any] = {"id": req_id}  # type: ignore[explicit-any]
                try:
                    if self._tool_executor:
                        exec_result = await self._tool_executor(tool_name, args)
                        result["result"] = exec_result
                    else:
                        result["error"] = "Tool executor not set"
                except Exception as exc:
                    logger.exception("Tool execution failed")
                    result["error"] = str(exc)

                response = json.dumps(result).encode("utf-8") + b"\n"
                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        """Stop the TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def set_tool_executor(self, executor: _ToolExecutor) -> None:
        """Set the tool executor callback."""
        self._tool_executor = executor


class QwenExecutor(Executor):
    """Executor that drives Qwen Code via its RPC mode.

    Spawns a ``qwen`` subprocess in RPC mode and manages sessions through
    its JSONL protocol. Tools are bridged via MCP tool registration.
    """

    def __init__(
        self,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,  # OSEnvSpec imported later to avoid circular deps
        model: str | None = None,
        qwen_path: str | None = None,
    ) -> None:
        """Initialize the Qwen executor.

        :param cwd: Working directory for the qwen subprocess.
        :param os_env: Environment spec for sandboxing.
        :param model: Model identifier to use.
        :param qwen_path: Absolute path to qwen CLI binary. Defaults to "qwen".
        """
        self._cwd = cwd
        self._os_env = os_env
        self._model = model
        self._qwen_path = qwen_path or "qwen"
        self._process: subprocess.Popen[str] | None = None
        self._tool_server: _ToolServer | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()  # type: ignore[explicit-any]
        self._session_id: str | None = None
        self._messages_sent: bool = False

    async def start_process(self) -> None:
        """Start the qwen subprocess in RPC mode."""
        cmd = [self._qwen_path, "--mode", "rpc"]

        env = os.environ.copy()

        # Start the process
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=self._cwd,
        )

        # Start reader task
        self._reader_task = asyncio.create_task(self._read_output())

    async def _read_output(self) -> None:
        """Read JSONL output from qwen subprocess."""
        if not self._process or not self._process.stdout:
            return

        try:
            for line in self._process.stdout:
                if not line:
                    break
                try:
                    event = json.loads(line.strip())
                    await self._queue.put(event)
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            logger.exception("Error reading qwen output")
            await self._queue.put({"type": "error", "message": str(exc)})

    async def send_request(self, request: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Send a JSON request to qwen."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("Process not started")

        line = json.dumps(request) + "\n"
        self._process.stdin.write(line)
        await self._process.stdin.drain()  # type: ignore[attr-defined]

    async def receive_event(self, timeout: float | None = 5.0) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Receive an event from qwen with optional timeout."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timeout waiting for qwen response") from exc

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn of the agent loop."""
        # Start process if not already running
        if self._process is None:
            await self.start_process()

        # Ensure tool server is running
        if self._tool_server is None:
            self._tool_server = _ToolServer()
            await self._tool_server.start()

        # Create session if needed
        if self._session_id is None:
            self._session_id = secrets.token_urlsafe(16)

        # Setup tool server with executor
        def get_tool_executor() -> _ToolExecutor | None:
            return getattr(self, "_tool_call_handler", None)

        if self._tool_server:
            self._tool_server.set_tool_executor(get_tool_executor())  # type: ignore[arg-type]

        # Build MCP tools config
        mcp_tools = []
        for tool in tools:
            mcp_tools.append({
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            })

        # Send session start/continue message
        request: dict[str, Any] = {  # type: ignore[explicit-any]
            "id": self._session_id,
            "type": "session_continue" if self._messages_sent else "session_start",
            "model": config.model if config and config.model else self._model or "auto",
            "systemPrompt": system_prompt,
            "messages": messages,
            "tools": mcp_tools,
        }

        await self.send_request(request)
        self._messages_sent = True

        # Process responses
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ExecutorError(message="Timeout waiting for qwen response", retryable=True)
                return

            event_type = event.get("type", "")

            if event_type == "text_delta":
                content = event.get("content", "")
                if content:
                    yield TextChunk(text=content)

            elif event_type == "tool_call":
                tool_name = event.get("name", "")
                tool_args = event.get("arguments", {})
                yield ToolCallRequest(
                    name=tool_name,
                    args=tool_args or {},
                )

            elif event_type == "turn_complete":
                text = event.get("text", "")
                yield TurnComplete(response=text)
                return

            elif event_type == "error":
                error_msg = event.get("message", "Unknown error")
                yield ExecutorError(message=error_msg, retryable=True)
                return

    async def close_session(self, session_key: str) -> None:
        """Close a session."""
        # No-op for this executor

    async def close(self) -> None:
        """Close the executor and cleanup resources."""
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

        if self._tool_server:
            await self._tool_server.close()
            self._tool_server = None

        if self._process:
            try:
                self._process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    self._process.kill()
            finally:
                self._process = None
