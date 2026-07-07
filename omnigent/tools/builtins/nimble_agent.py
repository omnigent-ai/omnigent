"""Built-in tool: nimble_agent — Nimble Web Search Agent (WSA) → structured JSON.

Runs a Nimble Web Search Agent (``POST /v1/agent``) and returns the agent's
structured, parsed results (e.g. Google SERP entities) as JSON in one call. The
WSA template is selected via the ``agent`` config value (default
``google_search``); the LLM supplies the ``query``.

Configured in the agent spec::

    tools:
      builtins:
        - name: nimble_agent
          api_key: ${NIMBLE_API_KEY}
          # optional:
          # agent: google_search   # the Nimble WSA template (default)

See https://docs.nimbleway.com/api-reference/agent
"""

from __future__ import annotations

import json
import logging
import os

# Any: Nimble's JSON response is a heterogeneous dict with string keys and
# mixed value types (str, dict, list, None).
from typing import Any

import httpx

from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)

_DEFAULT_NIMBLE_AGENT_URL = "https://sdk.nimbleway.com/v1/agent"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (same convention as the web_search / web_fetch / web_map backends).
_CLIENT_SOURCE = "omnigent"

# Default Nimble WSA template. ``google_search`` returns structured Google SERP entities.
_DEFAULT_AGENT = "google_search"

# WSA runs a browser task; allow a generous read timeout.
_AGENT_TIMEOUT_S = 90.0

# Cap the returned JSON so a large SERP cannot blow the model context.
_MAX_CONTENT_CHARS = 50_000

_DESCRIPTION = (
    "Run a Nimble Web Search Agent to get structured, parsed results (e.g. Google "
    "SERP entities — organic results, ads, related searches) for a query in one "
    "call. Returns structured JSON, unlike web_search which returns a text list."
)


def _agent_url() -> str:
    """Resolve the Nimble Agent URL; ``OMNIGENT_NIMBLE_AGENT_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_NIMBLE_AGENT_URL", _DEFAULT_NIMBLE_AGENT_URL)


def _run_agent_nimble(query: str, config: dict[str, str]) -> str:
    """
    Run a Nimble Web Search Agent for a query and return its structured results.

    :param query: The search query.
    :param config: Spec-level config; ``api_key`` (required), optional ``agent``
        (the WSA template, default ``google_search``).
    :returns: Structured JSON (the agent's parsed entities), or an error message.
        Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the nimble_agent config in config.yaml."
    agent = config.get("agent") or _DEFAULT_AGENT
    try:
        resp = httpx.post(
            _agent_url(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json={"agent": agent, "params": {"query": query}},
            timeout=_AGENT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Nimble agent error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Nimble agent error: {exc}"
    return _format_agent(resp.json())


def _truncate(text: str) -> str:
    """Cap text to keep the model context bounded."""
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + "\n\n[truncated]"
    return text


def _format_agent(data: dict[str, Any]) -> str:
    """
    Format Nimble's ``/v1/agent`` response into structured JSON text for the LLM.

    Nimble returns ``{"data": {"parsing": {"entities": {...}}, "markdown"?,
    "html"?}, "status", "task_id", ...}``. Prefer the parsed ``entities`` as JSON;
    fall back to ``markdown`` / ``html``.

    :param data: The parsed JSON response from Nimble.
    :returns: Structured JSON (entities) or text, truncated; or a status message.
    """
    inner = data.get("data") or {}
    parsing = inner.get("parsing")
    if isinstance(parsing, dict):
        entities = parsing.get("entities")
        if entities:
            return _truncate(json.dumps(entities, ensure_ascii=False))
    body = inner.get("markdown") or inner.get("html") or ""
    if body.strip():
        return _truncate(body)
    status = data.get("status", "unknown")
    return f"No structured data returned by the Nimble agent (status: {status})."


class NimbleAgentTool(Tool):
    """
    Nimble Web Search Agent tool: WSA → structured JSON.

    :param config: Spec-level config, e.g. ``{"api_key": "...", "agent": "google_search"}``.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        :param config: Spec-level config with ``api_key`` and optional ``agent``.
        """
        self._config = config or {}

    @classmethod
    def name(cls) -> str:
        """:returns: ``"nimble_agent"``."""
        return "nimble_agent"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return _DESCRIPTION

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema for nimble_agent.

        :returns: A function tool schema with a required ``query`` parameter.
        """
        return {
            "type": "function",
            "function": {
                "name": "nimble_agent",
                "description": _DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run nimble_agent synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of this tool.
        :returns: ``False``.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a nimble_agent call.

        :param arguments: JSON-encoded dict with a ``query`` key.
        :param ctx: Tool execution context (unused).
        :returns: Structured JSON, or an error message.
        """
        del ctx
        parsed: dict[str, Any] = json.loads(arguments)
        query = parsed.get("query")
        if not query:
            return "Error: 'query' parameter is required."
        return _run_agent_nimble(str(query), self._config)
