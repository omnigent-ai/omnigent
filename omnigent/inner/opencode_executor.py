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

import json
import logging
import os
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
