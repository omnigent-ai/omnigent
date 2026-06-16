"""Agent Client Protocol (ACP) client for ``cursor-agent acp``.

``cursor-agent acp`` runs Cursor's agent as an ACP server: a JSON-RPC 2.0 peer
speaking newline-delimited JSON over stdio. A session stays open across turns,
so the harness drives it turn by turn rather than respawning the CLI.

Covers what the harness needs: ``initialize`` → ``session/new`` (with cwd,
model, MCP servers) → ``session/prompt`` (streaming ``session/update``
notifications) → ``session/cancel``. Responses are correlated by JSON-RPC id;
``session/update`` notifications are queued for the active prompt; server→client
requests are auto-replied (permission requests auto-allowed, anything else a
null result) so the server never blocks.

``session/new`` accepts ``{cwd, model, mcpServers}`` and echoes the resolved
model; ``session/prompt`` returns ``{stopReason}`` at end of turn and streams
``agent_message_chunk`` / ``agent_thought_chunk`` / ``tool_call`` updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from asyncio import Future, Queue, Task
from collections.abc import AsyncIterator, Sequence
from typing import Any, TypeAlias

from ._subprocess_lifecycle import close_subprocess_transport
from .executor import (
    ExecutorEvent,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    classify_tool_result,
)

logger = logging.getLogger(__name__)

# One ACP JSON-RPC message (request / response / notification). The schema is
# owned by the ACP spec + cursor-agent, so it is opaque JSON at this layer.
AcpMessage: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Body of one ``session/update`` notification (the inner ``params.update``
# object), e.g. an ``agent_message_chunk`` / ``tool_call`` / ``tool_call_update``.
AcpUpdate: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# ACP protocol revision this client negotiates (integer, per the ACP spec).
_ACP_PROTOCOL_VERSION = 1

# Read stdout in 64 KiB chunks and split on newlines ourselves so a single
# large notification line (e.g. a big tool result) can't overflow
# ``StreamReader.readline``'s 64 KiB cap — same robustness as the pi/cursor
# stream readers.
_STREAM_READ_CHUNK_SIZE = 65536

# Default per-request timeout. A single ``session/prompt`` can run the whole
# agent turn (tool loop, long generation), so it is generous; session
# setup calls resolve far quicker.
_REQUEST_TIMEOUT_SECONDS = 600.0


async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:  # type: ignore[explicit-any]
    """Indirection point for ``asyncio.create_subprocess_exec`` (test seam).

    Mirrors the seam in the pi / cursor executors so tests can stub
    subprocess creation by patching ``omnigent.inner.cursor_acp._create_subprocess_exec``
    without touching the global ``asyncio`` singleton.
    """
    return await asyncio.create_subprocess_exec(*args, **kwargs)


class AcpError(RuntimeError):
    """An ACP request returned a JSON-RPC error or the server died."""


class AcpClient:
    """A persistent JSON-RPC client over a ``cursor-agent acp`` subprocess."""

    def __init__(
        self,
        cursor_path: str,
        *,
        env: dict[str, str],
        cwd: str | None,
        extra_args: list[str] | None = None,
        subcommand: Sequence[str] = ("acp",),
    ) -> None:
        """
        :param cursor_path: Path to spawn — the ``cursor-agent`` binary.
        :param env: The COMPLETE subprocess environment (allowlisted by the
            caller; never the full ``os.environ``).
        :param cwd: Working directory for the ACP server, or ``None`` to inherit.
        :param extra_args: Extra CLI args appended after the ACP subcommand
            (e.g. ``["--sandbox", "enabled"]``). ``None`` appends nothing.
        :param subcommand: How the CLI is told to enter ACP server mode. Most
            ACP CLIs use an ``acp`` subcommand (the default ``("acp",)``);
            CLIs that use a flag can pass it here instead.
        """
        self._cursor_path = cursor_path
        self._env = env
        self._cwd = cwd
        self._extra_args = list(extra_args or [])
        self._subcommand = list(subcommand)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: Task[None] | None = None
        self._stderr_task: Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, Future[AcpMessage]] = {}
        self._notifications: Queue[AcpMessage] = Queue()
        self._stderr_lines: list[str] = []

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> AcpMessage:
        """Spawn ``cursor-agent acp`` and complete the ACP ``initialize`` handshake.

        :returns: The ``initialize`` result (agent capabilities, auth methods).
        :raises AcpError: If the process fails to start or initialize.
        """
        logger.debug(
            "AcpClient: spawning %s %s %s",
            self._cursor_path,
            " ".join(self._subcommand),
            " ".join(self._extra_args),
        )
        self._proc = await _create_subprocess_exec(
            self._cursor_path,
            *self._subcommand,
            *self._extra_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())
        return await self.request(
            "initialize",
            {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                # We do not expose host filesystem capabilities to the agent;
                # cursor-agent uses its own native file tools in its cwd.
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
        )

    async def new_session(
        self,
        *,
        cwd: str,
        model: str | None,
        mcp_servers: list[AcpMessage] | None = None,
    ) -> str:
        """Create a session and return its id.

        :param cwd: Working directory the agent operates in.
        :param model: Cursor model id to pin (e.g. ``"gpt-5.4-mini"``), or
            ``None`` to use cursor's configured default.
        :param mcp_servers: ACP ``mcpServers`` entries (http/sse), or ``None``.
        :returns: The new session id.
        """
        params: AcpMessage = {"cwd": cwd, "mcpServers": mcp_servers or []}
        if model:
            params["model"] = model
        result = await self.request("session/new", params)
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError(f"session/new returned no sessionId: {result!r}")
        return session_id

    async def prompt_stream(
        self,
        session_id: str,
        blocks: list[AcpMessage],
    ) -> AsyncIterator[tuple[str, AcpMessage]]:
        """Send ``session/prompt`` and stream the turn.

        Yields ``("update", update)`` for each ``session/update`` notification as
        it arrives, then exactly one terminal ``("result", {...stopReason...})``
        or ``("error", {...})`` when the prompt request resolves.

        :param session_id: The session to prompt.
        :param blocks: ACP prompt content blocks, e.g.
            ``[{"type": "text", "text": "..."}]``.
        """
        # Drain any stray notifications left over from a prior turn (idle
        # ``available_commands_update`` etc.) so they aren't misattributed.
        while not self._notifications.empty():
            self._notifications.get_nowait()

        rid = self._send("session/prompt", {"sessionId": session_id, "prompt": blocks})
        fut = self._pending[rid]

        while True:
            getter: Task[AcpMessage] = asyncio.ensure_future(self._notifications.get())
            done, _pending = await asyncio.wait({getter, fut}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield ("update", getter.result())
                continue
            # The prompt response landed; surface any already-queued updates
            # before the terminal item, then stop.
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await getter
            while not self._notifications.empty():
                yield ("update", self._notifications.get_nowait())
            resp = fut.result()
            if "error" in resp:
                yield ("error", resp["error"])
            else:
                result = resp.get("result")
                yield ("result", result if isinstance(result, dict) else {})
            return

    async def cancel(self, session_id: str) -> None:
        """Best-effort ``session/cancel`` notification to interrupt the turn."""
        if not self.running or self._proc is None or self._proc.stdin is None:
            return
        with contextlib.suppress(Exception):
            self._proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/cancel",
                            "params": {"sessionId": session_id},
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await self._proc.stdin.drain()

    async def request(self, method: str, params: AcpMessage) -> AcpMessage:
        """Send a JSON-RPC request and await its result.

        :raises AcpError: On a JSON-RPC error response, timeout, or dead server.
        """
        rid = self._send(method, params)
        try:
            resp = await asyncio.wait_for(self._pending[rid], timeout=_REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise AcpError(f"ACP {method} timed out") from exc
        if "error" in resp:
            raise AcpError(f"ACP {method} failed: {resp['error']}")
        result = resp.get("result")
        return result if isinstance(result, dict) else {}

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines)

    async def close(self) -> None:
        """Terminate the ACP subprocess and stop the reader tasks."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
        self._stderr_task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError, RuntimeError):
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
            close_subprocess_transport(self._proc)
            self._proc = None

    # -- internals ---------------------------------------------------------

    def _send(self, method: str, params: AcpMessage) -> int:
        """Write a JSON-RPC request; return its id (caller awaits ``_pending[id]``)."""
        if self._proc is None or self._proc.stdin is None:
            raise AcpError("ACP server is not running")
        self._next_id += 1
        rid = self._next_id
        fut: Future[AcpMessage] = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
            separators=(",", ":"),
        )
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        return rid

    def _reply(self, request_id: Any, result: AcpMessage) -> None:  # type: ignore[explicit-any]
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, separators=(",", ":")
        )
        self._proc.stdin.write((line + "\n").encode("utf-8"))

    async def _reader(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._iter_lines(self._proc.stdout):
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("AcpClient: non-JSON line: %s", text[:200])
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — reader logs and exits on any unexpected error
            logger.debug("AcpClient reader error: %s", exc)
        finally:
            # Fail any in-flight requests so awaiters don't hang on a dead server.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError("ACP server closed the connection"))

    def _dispatch(self, msg: AcpMessage) -> None:
        method = msg.get("method")
        if "id" in msg and method is None:
            # Response to one of our requests.
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if method is not None and "id" in msg:
            # Server→client request — auto-reply so the server never blocks.
            self._handle_server_request(msg)
            return
        if method == "session/update":
            # ACP nests the update object under params.update —
            # ``{"sessionId": ..., "update": {"sessionUpdate": ..., ...}}`` —
            # so queue the inner update (what the harness maps to events).
            params = msg.get("params", {})
            update = params.get("update") if isinstance(params, dict) else None
            if isinstance(update, dict):
                self._notifications.put_nowait(update)

    def _handle_server_request(self, msg: AcpMessage) -> None:
        method = msg.get("method", "")
        request_id = msg.get("id")
        if isinstance(method, str) and "permission" in method:
            # Auto-allow: the harness runs headless, so there is no human to
            # prompt. Pick an "allow"-flavored option, else the first option.
            params = msg.get("params", {})
            options = params.get("options", []) if isinstance(params, dict) else []
            option_id = _pick_allow_option(options)
            self._reply(request_id, {"outcome": {"outcome": "selected", "optionId": option_id}})
            return
        # Unknown server request (e.g. an fs op we declined capability for):
        # null result keeps the protocol moving without granting anything.
        self._reply(request_id, {})

    @staticmethod
    async def _iter_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
        buffer = bytearray()
        while True:
            chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                idx = buffer.find(b"\n")
                if idx < 0:
                    break
                line = bytes(buffer[: idx + 1])
                del buffer[: idx + 1]
                yield line
        if buffer:
            yield bytes(buffer)

    async def _stderr_reader(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            async for raw in self._iter_lines(self._proc.stderr):
                text = raw.decode("utf-8", errors="replace").rstrip("\n\r")
                if text:
                    logger.debug("cursor-agent acp stderr: %s", text)
                    if len(self._stderr_lines) < 50:
                        self._stderr_lines.append(text)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort drainer
            pass


def _pick_allow_option(options: list[AcpMessage]) -> str:
    """Choose an 'allow' permission option id from an ACP request's options.

    :param options: The ``options`` list from a ``session/request_permission``
        request (each ``{"optionId", "name", "kind"}``).
    :returns: The id of an allow-flavored option, the first option's id, or
        ``"allow"`` as a last resort.
    """
    for opt in options:
        if not isinstance(opt, dict):
            continue
        marker = f"{opt.get('kind', '')}{opt.get('optionId', '')}".lower()
        if "allow" in marker:
            opt_id = opt.get("optionId")
            if isinstance(opt_id, str):
                return opt_id
    if options and isinstance(options[0], dict):
        first = options[0].get("optionId")
        if isinstance(first, str):
            return first
    return "allow"


# ---------------------------------------------------------------------------
# session/update → ExecutorEvent
# ---------------------------------------------------------------------------
#
# Shared by every ACP-driven harness (for example ``mimo acp``). Originally
# lived in ``cursor_executor.py`` when cursor was our only ACP client; promoted
# here when upstream replaced cursor with the in-process Python SDK so other ACP
# wrappers no longer have to import a now-unrelated module.


def _update_to_event(update: AcpUpdate) -> ExecutorEvent | None:
    """Map one ACP ``session/update`` to an ExecutorEvent, or ``None`` to skip.

    :param update: The ``params.update`` object from a ``session/update``
        notification (carries a ``sessionUpdate`` discriminator).
    :returns: The mapped event, or ``None`` for updates with nothing to surface
        (mode changes, command lists, plans, echoed user input).
    """
    kind = update.get("sessionUpdate")
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None

    if kind == "agent_message_chunk":
        return TextChunk(text=text) if isinstance(text, str) and text else None
    if kind == "agent_thought_chunk":
        if isinstance(text, str) and text:
            return ReasoningChunk(delta=text, event_type="reasoning_text")
        return None
    if kind == "tool_call":
        raw_input = update.get("rawInput")
        return ToolCallRequest(
            name=str(update.get("title") or update.get("kind") or "tool"),
            args=raw_input if isinstance(raw_input, dict) else {},
            metadata={"call_id": update.get("toolCallId")},
        )
    if kind == "tool_call_update" and update.get("status") == "completed":
        result = update.get("content")
        classification = classify_tool_result(result)
        return ToolCallComplete(
            name=str(update.get("title") or "tool"),
            status=classification.status,
            result=result,
            error=classification.error or None,
            metadata={"call_id": update.get("toolCallId")},
        )
    return None
