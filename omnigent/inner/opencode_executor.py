"""OpenCodeExecutor: drive OpenCode through a persistent ``opencode serve`` process.

This executor keeps one long-lived ``opencode serve`` subprocess per Omnigent
conversation subprocess, communicating over HTTP/SSE via the ``opencode-ai``
Python SDK (``AsyncOpencode``).  The SDK/serve transport unlocks three
capabilities unavailable in the per-turn ``opencode run`` approach:

1. **Mid-turn interrupt** — ``client.session.abort(id=…)``.
2. **Live message queue** — a second ``client.session.chat(…)`` enqueues into
   a running session.
3. **In-process MCP tool-bridge** — Omnigent ``ToolSpec`` entries are exposed
   to OpenCode via a FastMCP server whose handlers round-trip through
   Omnigent's dispatch path.

Transport overview::

    HARNESS_OPENCODE_* env vars
            │
            ▼  (subprocess spawn)
    opencode_harness.create_app()   →  ExecutorAdapter(factory=OpenCodeExecutor)
            │
            ▼
    OpenCodeExecutor.run_turn(...)  →  yields TextChunk / ReasoningChunk /
                                        ToolCall* / TurnComplete / ExecutorError

The server boots lazily on the first ``run_turn`` call (after the MCP bridge
is ready, so its URL is known before ``OPENCODE_CONFIG_CONTENT`` is written).
The SDK client is built from the announced ``http://127.0.0.1:<port>`` URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from omnigent.inner.executor import (
    EnqueuedContent,
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
)

logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_OPENCODE_MODEL"
_ENV_CWD = "HARNESS_OPENCODE_CWD"
_ENV_OPENCODE_PATH = "HARNESS_OPENCODE_PATH"
_ENV_THINKING = "HARNESS_OPENCODE_THINKING"
_ENV_SKIP_PERMISSIONS = "HARNESS_OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS"
_ENV_GATEWAY_PROVIDER = "HARNESS_OPENCODE_GATEWAY_PROVIDER"
_ENV_GATEWAY_BASE_URL = "HARNESS_OPENCODE_GATEWAY_BASE_URL"
_ENV_GATEWAY_API_KEY = "HARNESS_OPENCODE_GATEWAY_API_KEY"
_ENV_MCP_SERVERS = "HARNESS_OPENCODE_MCP_SERVERS"
_OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
_OPENCODE_DISABLE_PROJECT_CONFIG_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"

_SERVER_BOOT_TIMEOUT_S = 30.0
# Read size for draining the server's stderr pipe. The output is discarded
# (it's only drained so the pipe never fills and blocks the subprocess).
_STDERR_READ_SIZE = 65536


def _parse_truthy(raw: str | None) -> bool:
    """Decode the truthy-env-var convention shared across harness wraps.

    :param raw: Raw env-var value or ``None``.
    :returns: ``True`` for ``"1"``/``"true"``/``"yes"``/``"on"`` (case-insensitive).
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_opencode_binary() -> str:
    """Return the absolute path to the ``opencode`` binary.

    :returns: ``HARNESS_OPENCODE_PATH`` if set, else ``shutil.which("opencode")``.
    :raises FileNotFoundError: No ``opencode`` binary located.
    """
    explicit = os.environ.get(_ENV_OPENCODE_PATH, "").strip()
    if explicit:
        return explicit
    found = shutil.which("opencode")
    if not found:
        raise FileNotFoundError(
            "opencode CLI not found on PATH. Install it from "
            "https://opencode.ai or set HARNESS_OPENCODE_PATH."
        )
    return found


def _split_provider_model(model: str | None) -> tuple[str | None, str | None]:
    """Split an OpenCode ``provider/model`` id into its parts.

    :param model: e.g. ``"anthropic/claude-sonnet-4-5"`` or ``"gpt-5"`` or ``None``.
    :returns: ``(provider_id, model_id)``; provider is ``None`` when no slash.
    """
    if not model:
        return (None, None)
    if "/" in model:
        provider, rest = model.split("/", 1)
        return (provider or None, rest or None)
    return (None, model)


def _latest_user_text(messages: list[Message]) -> str:
    """Extract the most recent user message as plain text.

    Multimodal blocks are dropped with a warning (deferred — see design doc).

    :param messages: Inner ``Message`` list for the turn.
    :returns: Latest user text, or ``""`` when none present.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"input_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif block.get("type") in {"input_image", "input_file", "input_audio"}:
                    logger.warning(
                        "opencode harness: dropping %s block; multimodal input "
                        "not yet plumbed through the SDK.",
                        block.get("type"),
                    )
            if parts:
                return "\n".join(parts)
    return ""


def _resolve_mcp_servers_env() -> dict[str, Any]:
    """Decode ``HARNESS_OPENCODE_MCP_SERVERS`` into the OpenCode MCP map.

    :returns: Decoded ``{server_name: info}`` map, or ``{}`` when unset.
    :raises ValueError: When set but not a JSON object.
    """
    raw = os.environ.get(_ENV_MCP_SERVERS, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_ENV_MCP_SERVERS} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{_ENV_MCP_SERVERS} must be a JSON object")
    return parsed


def _build_opencode_config_content(
    mcp_extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Synthesise the ``OPENCODE_CONFIG_CONTENT`` payload, if any.

    Combines the gateway provider override (``provider.<id>.options.baseURL`` /
    ``apiKey``) with the merged MCP map (env-supplied servers + the in-process
    bridge entry *mcp_extra*).

    :param mcp_extra: In-process bridge entries to merge into the ``mcp`` map.
    :returns: A config dict, or ``None`` when nothing is configured.
    """
    base_url = os.environ.get(_ENV_GATEWAY_BASE_URL, "").strip()
    api_key = os.environ.get(_ENV_GATEWAY_API_KEY, "").strip()
    mcp_servers = dict(_resolve_mcp_servers_env())
    if mcp_extra:
        mcp_servers.update(mcp_extra)

    if not base_url and not api_key and not mcp_servers:
        return None

    payload: dict[str, Any] = {}
    if base_url or api_key:
        provider_id = os.environ.get(_ENV_GATEWAY_PROVIDER, "").strip() or "anthropic"
        options: dict[str, Any] = {}
        if base_url:
            options["baseURL"] = base_url
        if api_key:
            options["apiKey"] = api_key
        payload["provider"] = {provider_id: {"options": options}}
    if mcp_servers:
        payload["mcp"] = mcp_servers
    return payload


class _PartTracker:
    """Per-turn state for diffing streamed parts.

    OpenCode re-sends the full part on every ``message.part.updated``; this
    tracks the last-seen text length per part id (for text/reasoning deltas)
    and which tool parts have already emitted a ``ToolCallRequest``.
    """

    def __init__(self) -> None:
        self._text_len: dict[str, int] = {}
        self._tool_requested: set[str] = set()
        self._tool_completed: set[str] = set()

    def text_delta(self, part_id: str, full_text: str) -> str:
        """Return only the unseen suffix of *full_text* for *part_id*."""
        seen = self._text_len.get(part_id, 0)
        if len(full_text) <= seen:
            return ""
        self._text_len[part_id] = len(full_text)
        return full_text[seen:]

    def mark_tool_requested(self, part_id: str) -> bool:
        """Return ``True`` the first time a tool part id is seen."""
        if part_id in self._tool_requested:
            return False
        self._tool_requested.add(part_id)
        return True

    def mark_tool_completed(self, part_id: str) -> bool:
        """Return ``True`` the first time a tool part completes/errors."""
        if part_id in self._tool_completed:
            return False
        self._tool_completed.add(part_id)
        return True


def _tokens_to_usage(tokens: dict[str, Any]) -> dict[str, Any]:
    """Map an OpenCode ``tokens`` object onto the Omnigent usage map.

    :param tokens: ``{"input", "output", "reasoning", "cache": {"read","write"}}``.
    :returns: ``{"input_tokens","output_tokens","total_tokens",
        "cache_read_input_tokens","cache_creation_input_tokens"}``.
    """
    cache = tokens.get("cache") or {}
    inp = int(tokens.get("input", 0) or 0)
    out = int(tokens.get("output", 0) or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cache_read_input_tokens": int(cache.get("read", 0) or 0),
        "cache_creation_input_tokens": int(cache.get("write", 0) or 0),
    }


def _translate_part_event(
    part: dict[str, Any],
    tracker: _PartTracker,
    *,
    emit_reasoning: bool,
) -> list[ExecutorEvent]:
    """Translate one ``message.part.updated`` part dict into inner events.

    :param part: The part, dumped to a dict (snake_case keys; ``callID`` alias
        preserved as ``callID`` or ``call_id``).
    :param tracker: Per-turn diff state.
    :param emit_reasoning: When ``False``, reasoning parts are dropped.
    :returns: Zero or more inner :class:`ExecutorEvent` instances.
    """
    ptype = part.get("type")
    pid = part.get("id") or ""

    if ptype == "text":
        delta = tracker.text_delta(pid, part.get("text") or "")
        return [TextChunk(text=delta)] if delta else []

    if ptype == "reasoning":
        if not emit_reasoning:
            return []
        delta = tracker.text_delta(pid, part.get("text") or "")
        return [ReasoningChunk(delta=delta, event_type="reasoning_text")] if delta else []

    if ptype == "tool":
        tool_name = part.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []
        call_id = part.get("callID") or part.get("call_id") or pid
        state = part.get("state") or {}
        status_str = state.get("status")
        events: list[ExecutorEvent] = []
        raw_input = state.get("input")
        tool_args: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        # Defer emitting ToolCallRequest until the state is past "pending".
        # The pending state carries no input, so emitting there always yields
        # args={}.  running/completed/error all have input available.
        if status_str != "pending" and tracker.mark_tool_requested(pid):
            events.append(
                ToolCallRequest(
                    name=tool_name,
                    args=tool_args,
                    metadata={"call_id": call_id},
                )
            )
        if status_str in {"completed", "error"} and tracker.mark_tool_completed(pid):
            status = ToolCallStatus.ERROR if status_str == "error" else ToolCallStatus.SUCCESS
            events.append(
                ToolCallComplete(
                    name=tool_name,
                    status=status,
                    result=state.get("output"),
                    error=state.get("error") if status == ToolCallStatus.ERROR else None,
                    metadata={"call_id": call_id},
                )
            )
        return events

    return []


_LISTEN_RE = re.compile(r"listening on (http://\S+)")


def _parse_listen_url(line: str) -> str | None:
    """Extract the base URL OpenCode announces on stdout.

    :param line: A stdout line, e.g.
        ``"opencode server listening on http://127.0.0.1:4096"``.
    :returns: The URL, or ``None`` when the line isn't the announce line.
    """
    match = _LISTEN_RE.search(line)
    return match.group(1) if match else None


class _OpenCodeServer:
    """Owns one ``opencode serve`` subprocess + its ``AsyncOpencode`` client."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self.base_url: str | None = None
        self.client: Any | None = None

    async def start(self, *, cwd: str | None, extra_env: dict[str, str]) -> None:
        """Spawn ``opencode serve``, discover its URL, build the SDK client.

        :param cwd: Working directory for the server (``--dir`` equivalent).
        :param extra_env: Extra env vars (e.g. ``OPENCODE_CONFIG_CONTENT``).
        :raises ImportError: If the optional ``opencode-ai`` SDK is not installed.
        :raises RuntimeError: If the server doesn't announce a URL in time.
        """
        try:
            from opencode_ai import AsyncOpencode  # lazy: optional dep
        except ImportError as exc:
            raise ImportError(
                "The 'opencode' harness needs the optional 'opencode-ai' "
                "Python SDK, which is not installed. Install it with "
                '`pip install "omnigent[opencode]"` (or `pip install --pre '
                "opencode-ai`)."
            ) from exc

        binary = _resolve_opencode_binary()
        env = dict(os.environ)
        env.update(extra_env)
        self._proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "--port",
            "0",
            "--hostname",
            "127.0.0.1",
            "--print-logs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env=env,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        async def _await_url() -> str:
            assert self._proc is not None and self._proc.stdout is not None
            while True:
                line_bytes = await self._proc.stdout.readline()
                if not line_bytes:
                    raise RuntimeError("opencode serve exited before announcing a URL")
                url = _parse_listen_url(line_bytes.decode("utf-8", "replace"))
                if url:
                    return url

        try:
            self.base_url = await asyncio.wait_for(_await_url(), timeout=_SERVER_BOOT_TIMEOUT_S)
        except (asyncio.TimeoutError, RuntimeError) as exc:
            await self.close()
            raise RuntimeError(f"opencode serve failed to start: {exc}") from exc
        self.client = AsyncOpencode(base_url=self.base_url)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            chunk = await self._proc.stderr.read(_STDERR_READ_SIZE)
            if not chunk:
                return

    async def close(self) -> None:
        """Close the SDK client and terminate the server subprocess."""
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
            self.client = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        self._proc = None
        self.base_url = None


# ---------------------------------------------------------------------------
# In-process MCP tool-bridge
# ---------------------------------------------------------------------------

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _pick_free_port() -> int:
    """Bind an ephemeral port, release it, and return the number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _OmnigentToolBridge:
    """In-process FastMCP server exposing Omnigent spec tools to OpenCode.

    Each spec tool becomes one MCP tool whose handler round-trips through the
    adapter-supplied *tool_executor* (which dispatches through Omnigent policy
    + execution and emits the function_call events).
    """

    def __init__(self, tools: list[ToolSpec], tool_executor: ToolExecutor) -> None:
        self._tools = tools
        self._tool_executor = tool_executor
        self._server: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None
        self._mcp: Any | None = None

    async def start(self) -> str:
        """Boot the MCP server on an ephemeral port; return its ``/mcp`` URL."""
        from mcp.server.fastmcp import FastMCP

        self._port = _pick_free_port()
        mcp = FastMCP("omnigent", host="127.0.0.1", port=self._port)
        self._mcp = mcp

        for spec in self._tools:
            self._register_tool(mcp, spec)

        app = mcp.streamable_http_app()
        import uvicorn

        # ws="none": the MCP streamable-HTTP transport is POST/SSE only, so
        # skip loading uvicorn's websockets protocol (avoids its third-party
        # DeprecationWarnings and a needless import).
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self._port, log_level="warning", ws="none"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Wait until uvicorn flips started (bounded ~5s). If it never does,
        # the returned URL would be dead — surface that instead of handing
        # OpenCode a port nothing is listening on.
        for _ in range(100):
            if getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.05)
        else:
            await self.close()
            raise RuntimeError("opencode tool-bridge MCP server failed to start within 5s")
        return f"http://127.0.0.1:{self._port}/mcp"

    def _register_tool(self, mcp: Any, spec: ToolSpec) -> None:
        import inspect as _inspect

        from mcp.server.fastmcp.tools.base import Tool

        name = spec.get("name")
        if not isinstance(name, str) or not name:
            return
        description = spec.get("description") or ""
        params_schema = spec.get("parameters")
        schema = params_schema if isinstance(params_schema, dict) else {}
        properties = schema.get("properties", {})
        required = set(schema.get("required", []) or [])
        executor = self._tool_executor

        async def _handler(**kwargs: Any) -> Any:
            # Forward only the args the model actually supplied (drop defaulted
            # None values for omitted optionals) so the dispatch path sees a
            # clean arg dict.
            forwarded = {k: v for k, v in kwargs.items() if v is not None}
            return await executor(name, forwarded)

        # Synthesize a real keyword-only signature from the spec schema so
        # FastMCP's func_metadata builds a valid arg model (call-time validation
        # passes and args forward correctly). Annotate each param ``Any``.
        params = []
        for pname in properties:
            default = _inspect.Parameter.empty if pname in required else None
            params.append(
                _inspect.Parameter(
                    pname,
                    _inspect.Parameter.KEYWORD_ONLY,
                    annotation=Any,
                    default=default,
                )
            )
        _handler.__signature__ = _inspect.Signature(params)  # type: ignore[attr-defined]
        _handler.__annotations__ = dict.fromkeys(properties, Any)

        tool = Tool.from_function(
            _handler, name=name, description=description, structured_output=False
        )
        # Advertise the ORIGINAL spec JSON schema (with real types) to clients —
        # the synthesized Any-typed signature loses per-field types, but call-time
        # validation only uses fn_metadata, so overriding the advertised schema is
        # safe and gives OpenCode the correct parameter types. Overriding with
        # an empty schema ({}) is benign, so no truthiness guard.
        if isinstance(schema, dict):
            tool.parameters = schema
        mcp._tool_manager._tools[name] = tool

    async def close(self) -> None:
        """Shut the MCP server down."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.TimeoutError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
            except Exception:  # noqa: BLE001
                pass
        self._server = None
        self._task = None
        self._mcp = None


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


class OpenCodeExecutor(Executor):
    """Drive OpenCode via a persistent ``opencode serve`` + the Python SDK.

    Keeps one long-lived ``opencode serve`` subprocess per Omnigent
    conversation subprocess.  The SDK/serve transport unlocks mid-turn
    interrupt, live message queue, and in-process MCP tool-bridge.
    """

    def __init__(self) -> None:
        self._model: str | None = os.environ.get(_ENV_MODEL, "").strip() or None
        self._cwd: str | None = os.environ.get(_ENV_CWD, "").strip() or None
        self._thinking: bool = _parse_truthy(os.environ.get(_ENV_THINKING))
        skip_raw = os.environ.get(_ENV_SKIP_PERMISSIONS)
        self._skip_permissions: bool = True if skip_raw is None else _parse_truthy(skip_raw)
        self._server = _OpenCodeServer()
        self._server_started: bool = False
        self._client: Any | None = None
        self._bridge: _OmnigentToolBridge | None = None
        self._session_ids: dict[str, str] = {}
        # Cached (provider_id, model_id) from the server's default provider list.
        # Populated lazily by _resolve_provider_model when no explicit pin is set.
        self._default_provider_model: tuple[str, str] | None = None
        # Set by ExecutorAdapter before the first run_turn.
        self._tool_executor: ToolExecutor | None = None
        self._lock = asyncio.Lock()

    async def _ensure_server(self, tools: list[ToolSpec]) -> None:
        """Lazily boot the opencode serve process + optional MCP tool-bridge.

        Protected by ``self._lock`` so concurrent first-turn calls from
        multiple sessions don't race on server startup.

        If the server was already started without the MCP bridge (e.g. by
        an eager :meth:`get_providers` call) and *tools* now require it,
        the server is restarted with the bridge config. This is a one-time
        cost: the restart only fires on the first real turn after a
        pre-turn provider query.

        :param tools: Tool specs advertised for this turn; empty when the
            agent has no tools or the harness handles them externally.
        """
        async with self._lock:
            needs_bridge = bool(tools and self._tool_executor is not None)
            if self._server_started:
                if not needs_bridge or self._bridge is not None:
                    return
                # Server was warmed up without bridge (e.g. get_providers);
                # restart with the MCP bridge so tools are available.
                await self._server.close()
                self._server_started = False
                self._client = None
                self._bridge = None
            mcp_extra: dict[str, Any] | None = None
            if needs_bridge:
                self._bridge = _OmnigentToolBridge(tools, self._tool_executor)
                url = await self._bridge.start()
                mcp_extra = {"omnigent": {"type": "remote", "url": url}}
            extra_env: dict[str, str] = {}
            payload = _build_opencode_config_content(mcp_extra=mcp_extra)
            if payload is not None:
                extra_env[_OPENCODE_CONFIG_CONTENT_ENV] = json.dumps(
                    payload, separators=(",", ":")
                )
                extra_env[_OPENCODE_DISABLE_PROJECT_CONFIG_ENV] = "1"
            await self._server.start(cwd=self._cwd, extra_env=extra_env)
            self._client = self._server.client
            self._server_started = True

    async def get_providers(self) -> list[dict[str, Any]]:
        """Return the list of providers and their models from the opencode server.

        Boots the opencode serve process if it hasn't been started yet
        (without the MCP tool-bridge — the first real turn will restart
        with the bridge if tools are needed).

        :returns: A list of ``{"id": str, "name": str, "models": list[{"id": str, "name": str}]}``
            dicts, one per configured provider.
        :raises RuntimeError: When the server can't be started or no
            providers are configured.
        """
        await self._ensure_server([])
        resp = await self._require_client().app.providers()
        result: list[dict[str, Any]] = []
        for provider in getattr(resp, "providers", []) or []:
            models: list[dict[str, str]] = []
            for model_id, model in (getattr(provider, "models", {}) or {}).items():
                models.append({"id": model_id, "name": getattr(model, "name", model_id)})
            result.append(
                {
                    "id": getattr(provider, "id", ""),
                    "name": getattr(provider, "name", getattr(provider, "id", "")),
                    "models": models,
                }
            )
        return result

    async def _resolve_provider_model(self, config_model: str | None) -> tuple[str, str]:
        """Resolve ``(provider_id, model_id)`` for ``session.chat``.

        Both fields are required by the SDK; this method always returns a
        fully-populated pair.

        Precedence: per-turn *config_model* > ``self._model`` (from
        ``HARNESS_OPENCODE_MODEL``) > the server's configured default
        provider/model (cached after first lookup).

        :param config_model: Per-turn model override from :class:`ExecutorConfig`.
        :returns: ``(provider_id, model_id)`` — both non-empty strings.
        :raises RuntimeError: When no providers are configured in OpenCode.
        """
        pinned = config_model or self._model
        provider_id, model_id = _split_provider_model(pinned)
        if provider_id and model_id:
            return provider_id, model_id
        # Need the server default for at least one half.
        if self._default_provider_model is None:
            resp = await self._require_client().app.providers()
            default_map: dict[str, str] = dict(getattr(resp, "default", {}) or {})
            if not default_map:
                raise RuntimeError("opencode: no providers configured; run `opencode auth login`")
            # default_map is {provider_id: default_model_id}; take the first entry.
            dprov, dmodel = next(iter(default_map.items()))
            self._default_provider_model = (dprov, dmodel)
        dprov, dmodel = self._default_provider_model
        return (provider_id or dprov, model_id or dmodel)

    def _session_key_for(self, messages: list[Message]) -> str | None:
        """Extract the Omnigent session key from the message list.

        :param messages: Inner ``Message`` list for the turn.
        :returns: The ``session_id`` value from the first message that has
            one, or ``None`` when none is present.
        """
        for message in messages:
            sid = message.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return None

    async def _opencode_session_id(self, session_key: str | None) -> str:
        """Return the OpenCode session id for *session_key*, creating one if needed.

        :param session_key: The Omnigent session key (from messages), or ``None``
            to use the ``"default"`` bucket.
        :returns: The OpenCode session id string.
        """
        key = session_key or "default"
        if key in self._session_ids:
            return self._session_ids[key]
        created = await self._require_client().session.create()
        sid = created.id
        self._session_ids[key] = sid
        return str(sid)

    def _require_client(self) -> Any:
        """Return the SDK client, asserting the server has been started.

        Narrows ``self._client`` from ``Any | None`` to non-None for callers
        that run after :meth:`_ensure_server`.

        :returns: The live ``AsyncOpencode`` client.
        :raises RuntimeError: When the server has not been started yet.
        """
        if self._client is None:
            raise RuntimeError("opencode: server not started")
        return self._client

    @staticmethod
    def _as_dict(obj: Any) -> dict[str, Any]:
        """Coerce an SDK object or dict to a plain dict.

        Uses ``model_dump(by_alias=False)`` to get snake_case keys from SDK
        Pydantic models; falls back to ``__dict__`` for simple objects.

        :param obj: Any SDK response object, dict, or simple ``__dict__`` holder.
        :returns: A plain ``dict``; never ``None``.
        """
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            return cast("dict[str, Any]", obj.model_dump(by_alias=False))
        return getattr(obj, "__dict__", {})

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn via the persistent opencode serve process.

        Subscribe to the global SSE event stream, then fire ``session.chat``
        as a background task.  Events are consumed until ``session.idle`` fires
        for our session id, at which point the chat task is awaited for token
        usage.

        :param messages: Full conversation history; the latest user message is
            extracted and sent as the chat prompt.
        :param tools: Tool specs to expose (via MCP bridge if non-empty).
        :param system_prompt: System-level instruction forwarded to OpenCode.
        :param config: Per-turn config overrides (model, temperature, etc.).
        :yields: :class:`TextChunk`, :class:`ToolCallRequest`,
            :class:`ToolCallComplete`, :class:`TurnComplete`, or
            :class:`ExecutorError`.
        """
        await self._ensure_server(tools)
        session_key = self._session_key_for(messages)
        prompt = _latest_user_text(messages)
        if not prompt:
            yield ExecutorError(message="opencode harness: no user message in request")
            return
        session_id = await self._opencode_session_id(session_key)

        # Correction (A): session.chat requires BOTH provider_id and model_id.
        provider_id, model_id = await self._resolve_provider_model(
            config.model if config else None
        )

        tracker = _PartTracker()
        stream = await self._require_client().event.list()
        chat_kwargs: dict[str, Any] = {
            "id": session_id,
            "parts": [{"type": "text", "text": prompt}],
            "provider_id": provider_id,
            "model_id": model_id,
        }
        if system_prompt:
            chat_kwargs["system"] = system_prompt
        chat_task = asyncio.create_task(self._require_client().session.chat(**chat_kwargs))

        final_text: list[str] = []
        usage: dict[str, Any] | None = None
        # Tracks whether we reached the normal end-of-turn (session.idle). Any
        # other exit — session.error return, an exception, or the caller
        # abandoning/cancelling the generator mid-stream — must cancel the
        # in-flight chat task rather than leave it orphaned ("Task was
        # destroyed but it is pending"). Only the idle path awaits it below
        # for its token usage.
        idle_seen = False
        try:
            async for raw in stream:
                evt = self._as_dict(raw)
                etype = evt.get("type")
                props = self._as_dict(evt.get("properties"))
                ev_session = props.get("session_id") or props.get("sessionID")
                if etype == "message.part.updated":
                    part = self._as_dict(props.get("part"))
                    # Filter parts from other sessions when a session_id is present.
                    part_session = part.get("session_id")
                    if part_session and part_session != session_id:
                        continue
                    # Correction (B): no reasoning part type in this SDK;
                    # emit_reasoning=self._thinking kept for future compat.
                    for out in _translate_part_event(part, tracker, emit_reasoning=self._thinking):
                        if isinstance(out, TextChunk):
                            final_text.append(out.text)
                        yield out
                elif etype == "session.error" and (ev_session in (None, session_id)):
                    err = self._as_dict(props.get("error"))
                    yield ExecutorError(message=f"opencode: {err.get('name') or err or 'error'}")
                    return
                elif etype == "session.idle" and ev_session == session_id:
                    idle_seen = True
                    break
        finally:
            with contextlib.suppress(Exception):
                await stream.close()
            # Reap the chat task on every non-idle exit so it can't outlive the
            # turn. The idle path leaves it intact to be awaited for usage.
            if not idle_seen and not chat_task.done():
                chat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await chat_task

        try:
            result = await chat_task
            tokens = self._as_dict(result).get("tokens")
            if isinstance(tokens, dict):
                usage = _tokens_to_usage(tokens)
        except Exception as exc:  # noqa: BLE001
            yield ExecutorError(message=f"opencode: chat failed: {exc}")
            return

        yield TurnComplete(response="".join(final_text) or None, usage=usage)

    def supports_streaming(self) -> bool:
        """Streaming is delivered via the SSE event bus."""
        return True

    def supports_tool_calling(self) -> bool:
        """OpenCode calls tools via its own internal agent loop."""
        return True

    def handles_tools_internally(self) -> bool:
        """OpenCode manages the tool-call / result cycle internally."""
        return True

    def supports_live_message_queue(self) -> bool:
        """A second ``session.chat`` can enqueue into a running session."""
        return True

    def max_context_tokens(self) -> int | None:
        """Context limit is model-dependent and not fixed here."""
        return None

    async def interrupt_session(self, session_key: str) -> bool:
        """Abort the running OpenCode session for *session_key*.

        :param session_key: The Omnigent session key identifying the session.
        :returns: ``True`` when the abort call succeeded, ``False`` when the
            session is unknown or the client is not ready.
        """
        sid = self._session_ids.get(session_key)
        if not sid or self._client is None:
            return False
        with contextlib.suppress(Exception):
            await self._client.session.abort(id=sid)
            return True
        return False

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Fire-and-forget a new message into an already-running session.

        Sends a second ``session.chat`` without waiting for it — OpenCode
        queues it behind the current in-flight turn.

        :param session_key: The Omnigent session key.
        :param content: Text or structured content to enqueue.
        :returns: ``True`` when the task was dispatched; ``False`` when the
            session is unknown or the client is not ready.
        """
        sid = self._session_ids.get(session_key)
        if not sid or self._client is None:
            return False
        text = content if isinstance(content, str) else str(content)
        # Correction (A): always provide both provider_id and model_id.
        provider_id, model_id = await self._resolve_provider_model(None)
        kwargs: dict[str, Any] = {
            "id": sid,
            "parts": [{"type": "text", "text": text}],
            "provider_id": provider_id,
            "model_id": model_id,
        }
        task = asyncio.create_task(self._client.session.chat(**kwargs))
        # Observe the result so a failed enqueue logs a warning instead of an
        # unretrievable "Task exception was never retrieved" at GC time.
        task.add_done_callback(self._log_enqueue_result)
        return True

    @staticmethod
    def _log_enqueue_result(task: asyncio.Task[Any]) -> None:
        """Done-callback that surfaces a failed fire-and-forget enqueue."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("opencode harness: enqueued message failed: %s", exc)

    async def close_session(self, session_key: str) -> None:
        """Drop the cached OpenCode session id for *session_key*.

        :param session_key: The Omnigent session key whose cache entry to remove.
        """
        self._session_ids.pop(session_key, None)

    async def close(self) -> None:
        """Release all resources: MCP bridge + opencode serve subprocess."""
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None
        await self._server.close()
        self._server_started = False
        self._client = None
