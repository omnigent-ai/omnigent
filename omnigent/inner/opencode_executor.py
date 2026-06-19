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
from collections.abc import AsyncIterator
from typing import Any

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
_STDERR_CHUNK_LIMIT = 65536


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
        provider, _, rest = model.partition("/")
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
        metadata = {"call_id": call_id}
        if tracker.mark_tool_requested(pid):
            events.append(
                ToolCallRequest(
                    name=tool_name,
                    args=state.get("input") if isinstance(state.get("input"), dict) else {},
                    metadata=dict(metadata),
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
                    metadata=dict(metadata),
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
        :raises RuntimeError: If the server doesn't announce a URL in time.
        """
        from opencode_ai import AsyncOpencode  # lazy: optional dep

        binary = _resolve_opencode_binary()
        env = dict(os.environ)
        env.update(extra_env)
        self._proc = await asyncio.create_subprocess_exec(
            binary, "serve", "--port", "0", "--hostname", "127.0.0.1", "--print-logs",
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
            chunk = await self._proc.stderr.read(4096)
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
