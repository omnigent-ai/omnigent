"""AcpExecutor: drive *any* agent that speaks the Agent Client Protocol (ACP).

ACP (agentclientprotocol.com) is an open, editor-agnostic protocol: a JSON-RPC
2.0 conversation over newline-delimited JSON on a subprocess's stdin/stdout. Its
whole premise is that the *client* need not know which agent it drives — Goose
(``goose acp``), Qwen Code (``qwen --acp``), Gemini CLI
(``gemini --experimental-acp``), Zed's Claude Code bridge
(``@zed-industries/claude-code-acp``) and any in-house agent all speak the same
wire.

This executor is the **generic** counterpart to the vendor-specific
:class:`~omnigent.inner.goose_executor.GooseExecutor` /
:class:`~omnigent.inner.qwen_executor.QwenExecutor`: it spawns whatever command a
user configured (:class:`AcpAgentConfig.command`) and speaks ACP against it. The
handful of things those two hardcode become config knobs here:

* ``command``            — the argv to launch (``shlex``-split; never a shell).
* ``session_id_mode``    — ``"server"`` (agent assigns the id, Goose-style) or
                           ``"client"`` (we generate it, Qwen-style).
* ``send_model_in_session_new`` / ``model`` — send a non-standard ``model`` field
                           in ``session/new`` (Qwen accepts it; most agents take
                           the model from their own config / the command's flags).

Protocol flow (identical for every ACP agent):
  1. ``initialize``     — handshake; learn ``agentCapabilities`` (image support).
  2. ``session/new``    — create/adopt a session id + ``cwd`` + ``mcpServers``.
  3. ``session/prompt`` — send a user turn; consume streaming ``session/update``
     notifications (``agent_message_chunk``, ``agent_thought_chunk``,
     ``tool_call`` / ``tool_call_update``), answer any server-initiated
     ``session/request_permission`` / ``fs/*`` requests, then read the final
     response (``stopReason`` + optional ``usage``).
  4. Re-use the same session id for later turns (the agent retains context).

The agent runs its own agent loop, tool execution, context window and compaction
internally. This executor translates the ACP event stream into Omnigent
:class:`ExecutorEvent`s and routes the agent's permission requests through
Omnigent's TOOL_CALL policy + human-consent elicitation.

Vs. the Goose executor this generalizes, it additionally: renders the agent's
tool calls as Omnigent tool cards (``tool_call`` → ``ToolCallRequest``,
``tool_call_update`` → ``ToolCallComplete``), forwards reasoning
(``agent_thought_chunk`` → ``ReasoningChunk``), and honors interrupts via the ACP
``session/cancel`` notification.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import secrets
import shlex
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from omnigent.inner._acp_omnigent_mcp import OmnigentAcpMcp
from omnigent.inner.agent_env import clean_agent_env, declared_passthrough
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    describe_exception,
)
from omnigent.inner.os_env import OSEnvironment, create_os_environment
from omnigent.process_logging import current_process_log_path, display_log_path

logger = logging.getLogger(__name__)

# ACP's JSON-RPC schema is agent-owned and extensible; consumers narrow fields
# before use.
_AcpJsonObject: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]


class _PolicyVerdict(Protocol):
    action: str


_PolicyEvaluator: TypeAlias = Callable[[str, _AcpJsonObject], Awaitable[_PolicyVerdict]]
_ElicitationHandler: TypeAlias = Callable[[str, _AcpJsonObject], Awaitable[bool]]
_ToolExecutor: TypeAlias = Callable[[str, _AcpJsonObject], Awaitable[_AcpJsonObject]]

# ACP error code an agent maps to a filesystem "not found" (ENOENT) when a
# delegated ``fs/read_text_file`` misses — the reference ACP client lib special-
# cases exactly this code. Any other code surfaces raw.
_ACP_RESOURCE_NOT_FOUND_CODE = -32002

# ACP protocol constants (JSON-RPC 2.0 method names).
_AGENT_METHOD_INITIALIZE = "initialize"
_AGENT_METHOD_SESSION_NEW = "session/new"
_AGENT_METHOD_SESSION_PROMPT = "session/prompt"

# Notification sent *from* the agent to the client (streaming progress).
_CLIENT_NOTIFICATION_SESSION_UPDATE = "session/update"
# Notification sent *from* the client to the agent to abort the current turn.
_CLIENT_NOTIFICATION_SESSION_CANCEL = "session/cancel"

# Server-initiated request methods (agent → client).
_AGENT_REQUEST_REQUEST_PERMISSION = "session/request_permission"

# session/update.update.sessionUpdate discriminator values we map.
_UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
_UPDATE_AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
_UPDATE_TOOL_CALL = "tool_call"
_UPDATE_TOOL_CALL_UPDATE = "tool_call_update"
_UPDATE_USAGE = "usage_update"
_UPDATE_CONFIG_OPTION = "config_option_update"

# ACP ``session/set_config_option`` — the standard warm-switch method. The
# option id for the model, and the param name the agent expects (``configId``,
# not ``optionId``).
_AGENT_METHOD_SET_CONFIG_OPTION = "session/set_config_option"
_CONFIG_OPTION_MODEL = "model"

# ACP tool-call lifecycle statuses (the terminal ones close a tool card).
_TOOL_STATUS_COMPLETED = "completed"
_TOOL_STATUS_FAILED = "failed"

# Idle (time-without-progress) timeout for a prompt turn, in seconds.
# Some ACP agents stay silent while an external interaction is pending, so
# this is configurable. Parsing is import-time and fail-loud: a malformed,
# non-positive, or non-finite value aborts the ACP child at startup.
_PROMPT_TIMEOUT_ENV = "HARNESS_ACP_PROMPT_TIMEOUT_S"
_PROMPT_TIMEOUT_ERR = f"{_PROMPT_TIMEOUT_ENV} must be a positive finite number of seconds"
try:
    _PROMPT_TIMEOUT_SECONDS = float(os.environ.get(_PROMPT_TIMEOUT_ENV, "300"))
except ValueError as exc:
    raise ValueError(_PROMPT_TIMEOUT_ERR) from exc
if not math.isfinite(_PROMPT_TIMEOUT_SECONDS) or _PROMPT_TIMEOUT_SECONDS <= 0:
    raise ValueError(_PROMPT_TIMEOUT_ERR)

# Idle timeout for the initial ACP handshake (initialize / session setup).
_INIT_TIMEOUT_SECONDS = 30.0

# Agent stderr kept for diagnostics: how many trailing lines to retain, how many
# to quote in a turn error, and the per-line cap (a chatty CLI can emit one
# enormous line, and the error goes in a UI toast).
_STDERR_RING_LINES = 20
_STDERR_QUOTED_LINES = 5
_STDERR_LINE_LIMIT = 500
_STDERR_QUOTED_LIMIT = 1000

# ACP protocol version this executor targets (matches Goose 1.38 / Qwen).
_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class AcpAgentConfig:
    """Identity of the ACP agent this executor drives.

    :param command: The command to launch, e.g. ``"gemini --experimental-acp"``.
        Split with :func:`shlex.split` into an argv and exec'd directly (never
        via a shell), so quoting works but ``$VAR`` / pipes / redirects do not.
    :param name: Human label for logs / elicitation cards (e.g. ``"Gemini CLI"``).
    :param model: Optional model id. Only sent to the agent when
        :attr:`send_model_in_session_new` is set; otherwise inert (the agent
        takes its model from its own config or from flags in ``command``).
    :param session_id_mode: ``"server"`` — the agent assigns the session id and
        we adopt it (Goose); ``"client"`` — we generate the id and send it
        (Qwen). Defaults to ``"server"``, the ACP-idiomatic shape.
    :param send_model_in_session_new: Send a non-standard ``model`` field in
        ``session/new``. Off by default because a strict agent may reject unknown
        params; enable per-agent for Qwen-shaped agents that honor it.
    :param omnigent_mcp: Expose Omnigent's builtin tools to the agent via
        ``session/new.mcpServers`` (the shared ``serve-mcp`` relay). On by
        default; the global ``OMNIGENT_ACP_MCP=0`` kill switch also disables it.
    :param env_passthrough: Environment variable *names* this agent may read at
        spawn, e.g. ``("XAI_API_KEY",)``. The spawn env is deny-by-default and
        this executor drives an arbitrary agent, so it cannot infer the family
        the agent authenticates with — an agent that reads a variable must name
        it here (or in ``os_env.sandbox.env_passthrough``) or it starts
        unauthenticated. Names only; values come from the host environment.
    """

    command: str
    name: str = "ACP agent"
    model: str | None = None
    session_id_mode: str = "server"
    send_model_in_session_new: bool = False
    omnigent_mcp: bool = True
    env_passthrough: tuple[str, ...] = ()


class _AcpRequestError(Exception):
    """A handler failure to return as a JSON-RPC error on a server request.

    Carries the JSON-RPC ``code`` / ``message`` so the dispatch in
    :meth:`AcpExecutor._respond_to_agent_request` can build the error reply
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
    because the file is absent. Used to map onto the ENOENT code so the model
    sees "file not found".
    """
    lowered = message.lower()
    return (
        "no such file" in lowered
        or "errno 2" in lowered
        or "not found" in lowered
        or "does not exist" in lowered
    )


def _inline_text_file_data(file_data: object) -> str:
    """Decode a text ``input_file`` ``file_data`` data URI into inline text.

    ``input_file`` blocks may carry a ``data:<mime>;base64,<payload>`` URI. Text
    files are decoded so the model sees their content; binary files (PDF, images)
    can't be inlined and return ``""``. A bare, non-data-URI string is treated as
    already-inline text.
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


def _parse_image_data_uri(data_uri: object) -> tuple[str, str] | None:
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


class AcpExecutor(Executor):
    """Executor that drives any ACP agent over JSON-RPC 2.0 on stdio."""

    def __init__(
        self,
        config: AcpAgentConfig,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
    ) -> None:
        """Initialize the generic ACP executor.

        :param config: The agent to drive (command + protocol knobs).
        :param cwd: Working directory for the agent subprocess. ``None`` inherits
            the caller's cwd.
        :param os_env: Environment / sandbox spec. When its ``sandbox`` is not
            ``"none"``, the whole agent process tree is wrapped in the platform
            sandbox (bwrap/seatbelt) at spawn — see :meth:`_sandbox_launch_path`.
        """
        self._config = config
        self._cwd = cwd or os.getcwd()
        self._os_env = os_env
        # Advertise ``clientCapabilities.fs`` so the agent delegates file
        # reads/writes back to us (executed through the Omnigent OSEnvironment,
        # which enforces the spec's sandbox read/write roots). Enabled only when
        # an os_env is configured and it isn't a ``fork`` env — a forked env
        # operates on a *copied* tree whose path diverges from the agent's cwd.
        self._fs_delegation: bool = os_env is not None and not bool(getattr(os_env, "fork", False))
        self._os_environment: OSEnvironment | None = None

        # Session config options the agent advertises (``mode``, ``model``, …)
        # and the live model value, both learned from ``config_option_update``.
        self._config_option_ids: set[str] = set()
        self._active_model: str | None = None
        # Latches off once an agent proves it can't warm-switch, so we don't
        # retry a failing request on every turn.
        self._model_switch_supported: bool = True

        # Parsed argv; the first token is the binary we resolve / sandbox.
        self._argv: list[str] = shlex.split(config.command)
        if not self._argv:
            raise ValueError("AcpAgentConfig.command is empty")

        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[_AcpJsonObject] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # Last few agent stderr lines, attached to a startup failure. An agent
        # that dies or stalls during the handshake usually explains why on
        # stderr ("no API key", "unknown flag"), and that text is the whole
        # diagnosis; without it the turn error names only the stalled RPC.
        self._recent_stderr: deque[str] = deque(maxlen=_STDERR_RING_LINES)
        # Serializes stdin writes: run_turn (prompt / request replies) and the
        # adapter's interrupt_session() write from different tasks.
        self._write_lock = asyncio.Lock()

        self._rpc_id: int = 0
        self._pending: dict[int, asyncio.Future[_AcpJsonObject]] = {}

        self._session_id: str | None = None
        self._initialized: bool = False
        self._image_supported: bool = False
        self._system_prompt_sent: bool = False

        # ACP toolCallId → tool name, so a later tool_call_update can close the
        # right tool card with the name from the originating tool_call.
        self._tool_names: dict[str, str] = {}

        # Context-window size (tokens) reported via ``usage_update``; surfaced by
        # :meth:`max_context_tokens` so the UI context meter fills.
        self._context_window: int | None = None

        # Bridges the ExecutorAdapter installs (by attribute) so the agent's
        # mid-turn ``session/request_permission`` routes through Omnigent's
        # TOOL_CALL policy + human-consent elicitation. ``None`` → no bridge
        # wired (standalone / unit tests) → permission falls back to allow.
        self._policy_evaluator: _PolicyEvaluator | None = None
        self._elicitation_handler: _ElicitationHandler | None = None
        # Adapter-injected tool-execution bridge (the same ``_tool_executor``
        # attribute the SDK harnesses use); backs the Omnigent MCP relay.
        self._tool_executor: _ToolExecutor | None = None

        # Omnigent-tool MCP bridge — exposes builtin tools to the agent via
        # session/new.mcpServers (lazily started at first session; torn down in
        # :meth:`close`). ``_omnigent_tools`` is captured each turn for the relay.
        self._mcp = OmnigentAcpMcp(label=config.name)
        self._omnigent_tools: list[ToolSpec] = []

    # ------------------------------------------------------------------
    # Low-level ACP transport
    # ------------------------------------------------------------------

    async def _start_process(self) -> None:
        """Start the configured ACP agent as an asyncio subprocess.

        The StreamReader limit is raised to 16 MiB so a large ``session/new``
        response or tool-output line can't hit the default 64 KiB per-line cap.
        """
        # Reset handshake state: this may be a restart after the previous
        # subprocess died. ``_initialized`` is a one-way latch.
        self._initialized = False
        self._image_supported = False
        env = self._build_spawn_env()
        launch_path, argv = self._sandbox_launch(tuple(env.keys()))
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

    def _sandbox_launch(self, spawn_env_names: tuple[str, ...]) -> tuple[str, list[str]]:
        """Return ``(launch_path, argv)`` — sandbox launcher or the bare binary.

        When ``os_env.sandbox`` requests confinement, wraps the agent binary in
        the platform sandbox so its whole process tree runs confined to the
        spec's read/write roots. Falls back to the bare binary (never blocks
        startup) when no sandbox is requested or the backend is unavailable.

        ponytail: a sandboxed *generic* agent gets only its binary dir (read),
        the cwd, and ``/tmp`` (write) — we can't know an arbitrary agent's config
        dir. An agent that must write its own config under a sandbox needs
        ``sandbox: none`` (the default) for now; per-agent write roots is a
        documented follow-up.
        """
        binary = self._argv[0]
        rest = self._argv[1:]
        os_env = self._os_env
        if os_env is None:
            return binary, rest
        sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
        if sandbox_spec.type == "none":
            return binary, rest
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
                return binary, rest
            resolved_bin = Path(binary)
            if resolved_bin.parent != Path(".") and resolved_bin.exists():
                sandbox = with_additional_read_roots(sandbox, [resolved_bin.resolve().parent])
            sandbox = with_additional_write_roots(sandbox, [Path("/tmp")])
            sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
            return create_exec_launcher(binary, sandbox), rest
        except (OSError, ImportError, NotImplementedError) as exc:
            logger.warning(
                "Could not apply sandbox for ACP agent %s; running unsandboxed: %s",
                self._config.name,
                exc,
            )
            return binary, rest

    def _startup_error_message(self, exc: BaseException) -> str:
        """Describe a handshake failure, quoting the agent's own stderr.

        Builds on :func:`describe_exception` (which keeps a bare
        ``TimeoutError`` from rendering blank), then appends what the agent
        printed — that text ("no API key", "unknown flag") is usually the actual
        diagnosis — and the log file so the full traceback is findable.
        """
        detail = describe_exception(exc)
        if self._recent_stderr:
            tail = " | ".join(list(self._recent_stderr)[-_STDERR_QUOTED_LINES:])
            # Cap here too, not just per line in the reader: this string ends up
            # in a UI toast, and the ring can hold several long lines.
            if len(tail) > _STDERR_QUOTED_LIMIT:
                tail = tail[:_STDERR_QUOTED_LIMIT] + "...[truncated]"
            detail = f"{detail}; {self._config.name} stderr: {tail}"
        log_path = current_process_log_path()
        if log_path is not None:
            detail = f"{detail} (harness log: {display_log_path(log_path)})"
        return detail

    async def _read_stderr(self) -> None:
        """Continuously drain the agent's stderr, logging each line at debug.

        Prevents a chatty CLI from filling the OS pipe buffer (~64 KiB) and
        stalling the turn. Also retains the trailing lines so a startup failure
        can quote what the agent said before it gave up.
        """
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw_line = await self._proc.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    if len(line) > _STDERR_LINE_LIMIT:
                        line = line[:_STDERR_LINE_LIMIT] + "...[truncated]"
                    self._recent_stderr.append(line)
                    logger.debug("acp[%s] stderr: %s", self._config.name, line)
        except asyncio.CancelledError:
            # Expected: close() cancels this reader task on teardown.
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("acp[%s] stderr reader stopped: %s", self._config.name, exc)

    async def _read_stdout(self) -> None:
        """Continuously read NDJSON lines from the agent's stdout.

        Responses (``id`` + no ``method``) resolve the matching ``_pending``
        future; notifications and server-initiated requests go on ``_queue`` for
        ``run_turn`` to consume.
        """
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw_line = await self._proc.stdout.readline()
                if not raw_line:
                    # EOF — the subprocess exited. Wake in-flight futures so
                    # run_turn fails fast instead of blocking until idle timeout.
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(EOFError("ACP subprocess closed stdout"))
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg: _AcpJsonObject = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "acp[%s]: non-JSON stdout line: %r", self._config.name, line[:200]
                    )
                    continue

                msg_id = msg.get("id")
                # Match a response by "id + no method": the agent's own requests
                # (session/request_permission) also carry an id, so the method
                # check prevents a colliding request from mis-resolving our future.
                if msg_id is not None and "method" not in msg and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    await self._queue.put(msg)
        except (asyncio.CancelledError, EOFError):
            # Expected during shutdown / after the subprocess closes stdout.
            pass
        except Exception as exc:
            logger.exception("acp[%s] stdout reader error: %s", self._config.name, exc)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            await self._queue.put({"type": "error", "message": describe_exception(exc)})

    async def _send(self, msg: _AcpJsonObject) -> None:
        """Write one newline-terminated JSON message to the agent's stdin."""
        assert self._proc and self._proc.stdin
        encoded = (json.dumps(msg) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(encoded)
            await self._proc.stdin.drain()

    async def _rpc(
        self,
        method: str,
        params: _AcpJsonObject,
        timeout: float = _INIT_TIMEOUT_SECONDS,
    ) -> _AcpJsonObject:
        """Send a JSON-RPC 2.0 request and await its response."""
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[_AcpJsonObject] = loop.create_future()
        self._pending[req_id] = fut

        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            # asyncio.TimeoutError carries no message, so a caller reporting it
            # by str() would surface a blank failure. Name the stalled call.
            raise TimeoutError(
                f"ACP agent {self._config.name!r} did not answer {method} "
                f"within {timeout:g}s (command: {self._config.command!r})"
            ) from exc

    # ------------------------------------------------------------------
    # ACP handshake
    # ------------------------------------------------------------------

    def _build_spawn_env(self) -> dict[str, str]:
        """The env handed to the generic ACP subprocess.

        Deny-by-default: base + the names declared by the agent's own config and
        by the spec's ``os_env.sandbox.env_passthrough``. No prefix family is
        added because the executor cannot know which vendor an arbitrary ACP
        agent belongs to; an agent that authenticates from a variable names it
        instead, which keeps every *other* provider's secret out.

        Kept as a named builder so the spawn-env canary can drive the real thing
        rather than a hand-copied prefix list. The canary constructs a bare
        executor carrying only what the builder reads, so the agent config is
        read defensively rather than assumed present.
        """
        config = getattr(self, "_config", None)
        return clean_agent_env(
            allow_prefixes=(),
            extra_allowed=(
                *getattr(config, "env_passthrough", ()),
                *declared_passthrough(self._os_env),
            ),
        )

    def _warn_initialize_failed(self, reason: str) -> None:
        """Point a failed handshake at the env allowlist.

        A generic ACP agent gets the base environment plus whatever
        ``os_env.sandbox.env_passthrough`` declares — nothing else, since the
        executor cannot know which variable an arbitrary agent authenticates
        with. An agent that reads e.g. ``GEMINI_API_KEY`` therefore starts
        unauthenticated and usually dies during ``initialize``. That looks like
        a protocol fault, so name the likely cause once here rather than let
        every operator rediscover it.
        """
        logger.warning(
            "acp initialize failed for %r: %s. If this agent authenticates from an "
            "environment variable, declare it in os_env.sandbox.env_passthrough — "
            "the spawn environment is filtered to the base set plus that list.",
            self._config.command,
            reason,
        )

    async def _ensure_initialized(self) -> None:
        """Perform the ``initialize`` handshake if not already done."""
        if self._initialized:
            return
        try:
            resp = await self._rpc(
                _AGENT_METHOD_INITIALIZE,
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientInfo": {"name": "omnigent", "version": "1.0"},
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
        except Exception as exc:
            # Covers the child dying or timing out before it answers.
            self._warn_initialize_failed(str(exc))
            raise
        if "error" in resp:
            message = resp["error"].get("message", resp["error"])
            self._warn_initialize_failed(str(message))
            raise RuntimeError(f"ACP initialize failed: {message}")
        prompt_caps = (
            (resp.get("result") or {}).get("agentCapabilities", {}).get("promptCapabilities", {})
        )
        self._image_supported = bool(prompt_caps.get("image"))
        self._initialized = True

    async def _ensure_session(self) -> str:
        """Create (or reuse) an ACP session, returning the session id.

        In ``server`` mode we send only ``cwd`` + ``mcpServers`` and adopt the id
        the agent returns. In ``client`` mode we generate the id and send it.
        ``mcpServers`` carries Omnigent's builtin tools (via the shared serve-mcp
        relay) unless disabled — see :class:`OmnigentAcpMcp`.
        """
        if self._session_id is not None:
            return self._session_id

        params: _AcpJsonObject = {
            "cwd": self._cwd,
            # ACP requires this field even when no per-session MCP servers are
            # configured. Keep it empty when Omnigent MCP is disabled.
            "mcpServers": [],
        }
        if self._config.omnigent_mcp:
            params["mcpServers"] = self._mcp.session_new_servers(
                tools=self._omnigent_tools,
                tool_executor=getattr(self, "_tool_executor", None),
                loop=asyncio.get_event_loop(),
                enabled=True,
            )
        client_id: str | None = None
        if self._config.session_id_mode == "client":
            client_id = secrets.token_urlsafe(16)
            params["sessionId"] = client_id
        if self._config.send_model_in_session_new and self._config.model:
            params["model"] = self._config.model

        resp = await self._rpc(_AGENT_METHOD_SESSION_NEW, params, timeout=_INIT_TIMEOUT_SECONDS)
        if "error" in resp:
            raise RuntimeError(
                f"ACP session/new failed: {resp['error'].get('message', resp['error'])}"
            )
        result = resp.get("result", {})
        server_session_id = result.get("sessionId") if isinstance(result, dict) else None
        session_id = server_session_id or client_id
        if not session_id:
            raise RuntimeError(
                "ACP session/new response missing sessionId: " + json.dumps(resp)[:200]
            )
        self._session_id = session_id
        return self._session_id

    # ------------------------------------------------------------------
    # Server-initiated requests (agent → client)
    # ------------------------------------------------------------------

    async def _respond_to_agent_request(self, request: _AcpJsonObject) -> None:
        """Answer a server-initiated ACP request from the agent.

        - ``session/request_permission`` — decide via Omnigent's TOOL_CALL policy
          + human-consent elicitation (:meth:`_decide_permission`), then select
          the matching allow/reject option. NOT a blind approve.
        - ``fs/read_text_file`` / ``fs/write_text_file`` — when fs delegation is
          advertised, execute through the Omnigent OSEnvironment so the spec's
          sandbox read/write roots are enforced. Off → never arrive.
        - anything else — reply with JSON-RPC ``method not found`` so the agent
          fails loudly rather than acting on empty data.
        """
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        logger.debug("acp[%s] agent request: method=%s id=%s", self._config.name, method, req_id)

        result: _AcpJsonObject | None = None
        error: _AcpJsonObject | None = None
        try:
            if method == _AGENT_REQUEST_REQUEST_PERMISSION:
                allow = await self._decide_permission(params)
                result = self._permission_outcome(params, allow=allow)
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
            logger.debug("acp[%s] agent request %s failed: %s", self._config.name, method, exc)
            error = {"code": -32603, "message": f"{method} failed: {exc}"}

        reply: _AcpJsonObject = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        await self._send(reply)

    # ------------------------------------------------------------------
    # Filesystem delegation (agent → client, when fs capability advertised)
    # ------------------------------------------------------------------

    async def _ensure_os_environment(self) -> OSEnvironment:
        """Lazily create the OSEnvironment backing fs delegation."""
        if self._os_environment is None:
            env = create_os_environment(self._os_env)
            if env is None:
                raise _AcpRequestError(-32603, "omnigent: no os_env for fs delegation")
            self._os_environment = env
        return self._os_environment

    async def _handle_fs_read(self, params: _AcpJsonObject) -> _AcpJsonObject:
        """Serve an ACP ``fs/read_text_file`` by reading through the OSEnvironment.

        ACP params ``{path, line?, limit?}`` (1-based start line, max line count;
        both optional → whole file) map onto :meth:`OSEnvironment.read`.
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

    async def _handle_fs_write(self, params: _AcpJsonObject) -> _AcpJsonObject:
        """Serve an ACP ``fs/write_text_file`` by writing through the OSEnvironment.

        ACP params ``{path, content}``; the write goes through the helper so the
        spec's sandbox write roots are enforced at the Python layer.
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

    # ------------------------------------------------------------------
    # Permission (session/request_permission) → policy + elicitation
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_call(params: _AcpJsonObject) -> tuple[str, _AcpJsonObject]:
        """Pull ``(tool_name, tool_input)`` from a ``session/request_permission``.

        ACP's ``toolCall`` carries a human ``title`` (e.g. ``"shell"``), a
        ``kind`` (e.g. ``"execute"``), and a ``rawInput`` dict. We prefer the
        title, else the kind. (Vendor-specific ``_meta`` tool names — e.g.
        Goose's ``_meta.goose.toolCall.toolName`` — are not read here; ``title``
        is the portable name every ACP agent supplies.)
        """
        tool_call = params.get("toolCall") or {}
        name = tool_call.get("title") or tool_call.get("kind") or "tool"
        args = tool_call.get("rawInput")
        if not isinstance(args, dict):
            args = {}
        return str(name), args

    async def _decide_permission(self, params: _AcpJsonObject) -> bool:
        """Decide allow/deny for a permission request — policy then elicitation.

        1. **TOOL_CALL policy** (:attr:`_policy_evaluator`): a hard
           ``POLICY_ACTION_DENY`` denies; ``POLICY_ACTION_ASK`` defers to
           elicitation (and **fails closed** when no handler is wired);
           ``ALLOW`` / unspecified falls through.
        2. **Human-consent elicitation** (:attr:`_elicitation_handler`): routes
           to the user via a web approval card and returns their accept/deny.

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
                logger.warning("acp TOOL_CALL policy eval failed for %s: %s", tool_name, exc)
                action = None
            if action == "POLICY_ACTION_DENY":
                logger.info("acp permission denied by policy: tool=%s", tool_name)
                return False
            if action == "POLICY_ACTION_ASK":
                if handler is None:
                    logger.warning(
                        "acp TOOL_CALL policy ASK with no elicitation handler; denying tool=%s",
                        tool_name,
                    )
                    return False
                allowed = bool(await handler(tool_name, tool_input))
                logger.info(
                    "acp permission %s by user (policy ASK): tool=%s",
                    "allowed" if allowed else "denied",
                    tool_name,
                )
                return allowed
            # ALLOW / UNSPECIFIED / unknown → fall through to elicitation.

        if handler is not None:
            allowed = bool(await handler(tool_name, tool_input))
            logger.info(
                "acp permission %s by user: tool=%s",
                "allowed" if allowed else "denied",
                tool_name,
            )
            return allowed

        logger.debug("acp permission allowed (no policy/elicitation wired): tool=%s", tool_name)
        return True

    @staticmethod
    def _permission_outcome(params: _AcpJsonObject, *, allow: bool) -> _AcpJsonObject:
        """Map an allow/deny decision to an ACP permission ``outcome``.

        On allow, prefer a once-scoped grant (``allow_once``) over
        ``allow_always`` so we never persist a blanket "always allow". On deny,
        pick a ``reject_*`` option, or ``cancelled`` when none is offered. The
        agent's options carry both ``optionId`` and ``kind`` (e.g. ``allow_once``).
        """
        options = [o for o in (params.get("options") or []) if isinstance(o, dict)]

        def _pick(*kinds: str) -> _AcpJsonObject | None:
            for kind in kinds:
                for opt in options:
                    if opt.get("kind") == kind:
                        return opt
            return None

        if allow:
            chosen = _pick("allow_once", "allow_always") or next(
                (o for o in options if "allow" in str(o.get("kind", ""))), None
            )
        else:
            chosen = _pick("reject_once", "reject_always") or next(
                (o for o in options if "reject" in str(o.get("kind", ""))), None
            )
        if chosen is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _image_blocks_from_content(content: object) -> list[_AcpJsonObject]:
        """Build ACP ``image`` prompt blocks from a message's ``input_image`` blocks."""
        out: list[_AcpJsonObject] = []
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
        blocks: list[object],
        *,
        emit_image_marker: bool = False,
    ) -> str:
        """Extract prompt text from a Responses-API content-block list.

        ACP's ``session/prompt`` text part is plain text, so each block is folded:
        ``input_text``/``output_text``/``text`` verbatim; ``input_file`` inlined
        (fenced) when the runner resolved it to a text data URI, else a marker;
        ``input_image`` as a marker only when *emit_image_marker* is set.
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
    def _history_prefix(cls, prior: Sequence[object]) -> str:
        """Serialize prior conversation turns into a text prefix.

        On a *fresh* ACP session (the first turn of a newly spawned/respawned
        process, or after a session reset) the agent holds none of the earlier
        conversation. Since :meth:`run_turn` normally sends only the latest user
        turn, we'd lose everything before the switch. Replaying the transcript as
        a labeled ``role: content`` block restores that context.
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

    def handles_tools_internally(self) -> bool:
        """True — the ACP agent runs its own tool loop.

        The Session must NOT re-execute the ``ToolCallRequest`` /
        ``ToolCallComplete`` events we emit from ``tool_call`` updates; they are
        informational (they render tool cards showing what the agent did).
        """
        return True

    def supports_streaming(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        """Return the agent's reported context-window size, if observed yet."""
        return self._context_window

    @staticmethod
    def _usage_from_result(result: _AcpJsonObject) -> dict[str, int] | None:
        """Map an agent's final ``result.usage`` to Omnigent's usage keys.

        ACP does not standardize usage, but agents that report it (Goose) use
        ``{totalTokens, inputTokens, outputTokens}``; Omnigent's
        ``TurnComplete.usage`` uses ``{input_tokens, output_tokens, total_tokens}``.
        Absent → ``None`` (usage simply isn't shown for agents that don't report).
        """
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        out: dict[str, int] = {}
        if isinstance(usage.get("inputTokens"), int):
            out["input_tokens"] = usage["inputTokens"]
        if isinstance(usage.get("outputTokens"), int):
            out["output_tokens"] = usage["outputTokens"]
        if isinstance(usage.get("totalTokens"), int):
            out["total_tokens"] = usage["totalTokens"]
        return out or None

    def _handle_session_update(self, update: _AcpJsonObject) -> list[ExecutorEvent]:
        """Translate one ``session/update`` payload into ExecutorEvents.

        Returns the events to yield (usually 0 or 1). Side effects: records the
        context window from ``usage_update`` and tracks tool-call names so a
        later ``tool_call_update`` can close the right card.
        """
        update_type = update.get("sessionUpdate", "")
        events: list[ExecutorEvent] = []

        if update_type == _UPDATE_AGENT_MESSAGE_CHUNK:
            content = update.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else ""
            if text:
                events.append(TextChunk(text=text))
        elif update_type == _UPDATE_AGENT_THOUGHT_CHUNK:
            content = update.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else ""
            if text:
                events.append(ReasoningChunk(delta=text, event_type="reasoning_text"))
        elif update_type == _UPDATE_TOOL_CALL:
            call_id = update.get("toolCallId")
            name = update.get("title") or update.get("kind") or "tool"
            raw_input = update.get("rawInput")
            args = raw_input if isinstance(raw_input, dict) else {}
            if isinstance(call_id, str) and call_id:
                self._tool_names[call_id] = str(name)
                events.append(
                    ToolCallRequest(name=str(name), args=args, metadata={"call_id": call_id})
                )
        elif update_type == _UPDATE_TOOL_CALL_UPDATE:
            call_id = update.get("toolCallId")
            status = update.get("status")
            if isinstance(call_id, str) and status in (
                _TOOL_STATUS_COMPLETED,
                _TOOL_STATUS_FAILED,
            ):
                name = self._tool_names.pop(call_id, "tool")
                events.append(
                    ToolCallComplete(
                        name=name,
                        status=(
                            ToolCallStatus.SUCCESS
                            if status == _TOOL_STATUS_COMPLETED
                            else ToolCallStatus.ERROR
                        ),
                        result=update.get("content") or update.get("rawOutput"),
                        metadata={"call_id": call_id},
                    )
                )
        elif update_type == _UPDATE_USAGE:
            size = update.get("size")
            if isinstance(size, int) and size > 0:
                self._context_window = size

        elif update_type == _UPDATE_CONFIG_OPTION:
            self._note_config_options(update.get("configOptions"))

        return events

    def _note_config_options(self, options: object) -> str | None:
        """Record which session config options the agent exposes, and their values.

        ACP agents advertise settable options (``mode``, ``model``, …) via
        ``config_option_update``. Tracking the ids tells us whether a warm model
        switch is possible at all; tracking ``model``'s ``currentValue`` is the
        only trustworthy record of which model is live — an agent's own
        self-report is unreliable.

        :returns: The echoed ``model`` ``currentValue`` when the payload carried a
            model option, else ``None`` — lets a caller distinguish "the agent
            reported its model" from "no model option present".
        """
        if not isinstance(options, list):
            return None
        model_value: str | None = None
        for opt in options:
            if not isinstance(opt, dict):
                continue
            opt_id = opt.get("id")
            if not isinstance(opt_id, str):
                continue
            self._config_option_ids.add(opt_id)
            if opt_id == _CONFIG_OPTION_MODEL:
                current = opt.get("currentValue")
                if isinstance(current, str) and current:
                    self._active_model = current
                    model_value = current
        return model_value

    async def _apply_model_override(self, session_id: str, model: str | None) -> None:
        """Warm-switch the agent's model via ACP ``session/set_config_option``.

        Standard ACP (not a vendor extension), so this works for any agent that
        exposes a ``model`` session config option. The transcript is preserved —
        the session is not recreated — so a ``/model`` pick mid-conversation keeps
        the history the agent has already built up.

        No-ops when the model is unset, already active, or the agent never
        advertised a ``model`` option. A failed attempt latches the feature off
        for this process rather than re-requesting on every turn, and never fails
        the turn: an agent that can't switch should still answer on the model it
        has.

        :param session_id: The live ACP session to reconfigure.
        :param model: Requested model id, or ``None`` to leave it alone.
        """
        if not model or model == self._active_model or not self._model_switch_supported:
            return
        # Before the first ``config_option_update`` we don't know what's settable;
        # attempting is harmless because a rejection just latches the feature off.
        if self._config_option_ids and _CONFIG_OPTION_MODEL not in self._config_option_ids:
            self._model_switch_supported = False
            logger.info(
                "acp[%s] agent exposes no %r config option; leaving model as-is",
                self._config.name,
                _CONFIG_OPTION_MODEL,
            )
            return

        response = await self._rpc(
            _AGENT_METHOD_SET_CONFIG_OPTION,
            {"sessionId": session_id, "configId": _CONFIG_OPTION_MODEL, "value": model},
        )
        if "error" in response:
            self._model_switch_supported = False
            logger.warning(
                "acp[%s] model switch to %s rejected (%s); continuing on the current model",
                self._config.name,
                model,
                response["error"].get("message", response["error"]),
            )
            return
        # The agent echoes its options back; trust the echoed ``currentValue``
        # over our request, since an agent may normalize or silently reject the
        # id. Fall back to the requested model only when the agent echoed no model
        # option at all (some accept the switch without echoing options) — never
        # overwrite an echoed value with the request, or a later turn would skip a
        # switch it should retry.
        result = response.get("result")
        echoed_model = (
            self._note_config_options(result.get("configOptions"))
            if isinstance(result, dict)
            else None
        )
        if echoed_model is None:
            self._active_model = model
        logger.info(
            "acp[%s] model set to %s (transcript kept)", self._config.name, self._active_model
        )

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn of the agent loop via ACP.

        Sends ``session/prompt`` and yields streaming events (text, reasoning,
        tool-call cards) as the agent works, answering any
        ``session/request_permission`` mid-turn, until the final response
        (``stopReason``) arrives — then yields ``TurnComplete`` with usage.

        ``tools`` (Omnigent's builtin tool schemas) are captured for the Omnigent
        MCP relay set up at ``session/new`` — the agent still runs its OWN tools.
        """
        # Captured before the (lazy) session so the MCP relay can advertise them.
        self._omnigent_tools = tools or []
        try:
            if self._proc is None or self._proc.returncode is not None:
                await self._start_process()
            await self._ensure_initialized()
            session_id = await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            yield ExecutorError(message=self._startup_error_message(exc), retryable=False)
            return

        # Apply a ``/model`` pick to the live session before prompting, so the
        # switch takes effect on this turn with the transcript intact. Never fatal
        # — an agent that can't switch answers on the model it already has.
        requested_model = config.model if config is not None else None
        try:
            await self._apply_model_override(session_id, requested_model)
        except Exception as exc:  # noqa: BLE001
            self._model_switch_supported = False
            logger.warning("acp[%s] model switch failed: %s", self._config.name, exc)

        # A fresh ACP session holds no prior context. Captured before the latch
        # flips so we know whether to replay history into this turn.
        fresh_session = not self._system_prompt_sent

        user_text = ""
        image_blocks: list[_AcpJsonObject] = []
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

        # On a fresh session, replay prior conversation so a subprocess restart
        # (after the agent process exits) or a session reset doesn't drop the
        # thread. A ``/model`` switch no longer respawns — it reconfigures the
        # live session (see ``_apply_model_override``) — so it never reaches here.
        if fresh_session and latest_user_idx is not None and latest_user_idx > 0:
            history_prefix = self._history_prefix(messages[:latest_user_idx])
            user_text = f"{history_prefix}\n\nuser: {user_text}" if user_text else history_prefix

        # ACP has no system-prompt field, so fold it into the first turn.
        if fresh_session:
            if system_prompt:
                user_text = f"{system_prompt}\n\n{user_text}" if user_text else system_prompt
            self._system_prompt_sent = True

        prompt_blocks: list[_AcpJsonObject] = []
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
        fut: asyncio.Future[_AcpJsonObject] = loop.create_future()
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
                yield ExecutorError(message="Timeout waiting for ACP response", retryable=True)
                return

            # Complete only once the future is resolved AND the queue is drained,
            # so trailing chunks aren't truncated.
            if fut.done() and self._queue.empty():
                try:
                    response = fut.result()
                except Exception as exc:  # noqa: BLE001
                    self._session_id = None
                    self._system_prompt_sent = False
                    yield ExecutorError(message=f"ACP process error: {exc}", retryable=True)
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
                for event in self._handle_session_update(update):
                    if isinstance(event, TextChunk):
                        accumulated_text.append(event.text)
                    yield event
            elif notification.get("id") is not None and notification.get("method"):
                # Server-initiated request (session/request_permission / fs/*):
                # routes through policy + elicitation. Blocks while the human decides.
                await self._respond_to_agent_request(notification)

            # Inbound message = progress; reset the idle deadline.
            deadline = loop.time() + _PROMPT_TIMEOUT_SECONDS

    async def interrupt_session(self, session_key: str) -> bool:  # noqa: ARG002 — one ACP session per process
        """Abort the running turn via the ACP ``session/cancel`` notification.

        The agent responds by ending the in-flight ``session/prompt`` with a
        ``cancelled`` stop reason, which the ``run_turn`` loop then surfaces.
        Best-effort: a no-op (returns ``False``) if there's no live session.
        """
        if self._proc is None or self._proc.returncode is not None or self._session_id is None:
            return False
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": _CLIENT_NOTIFICATION_SESSION_CANCEL,
                    "params": {"sessionId": self._session_id},
                }
            )
            return True
        except Exception as exc:  # noqa: BLE001 — interrupt is best-effort
            logger.debug("acp[%s] session/cancel failed: %s", self._config.name, exc)
            return False

    async def close_session(self, session_key: str) -> None:
        """Close a named session (no-op; the ACP session is per-process)."""

    async def close(self) -> None:
        """Terminate the agent subprocess and clean up."""
        # Tear down the Omnigent MCP relay HTTP server + its bridge dir first.
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
