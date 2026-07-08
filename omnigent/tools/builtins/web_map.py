"""Built-in tool: web_map — website URL discovery via Nimble.

Maps the URLs on a website using Nimble's Map endpoint (``POST /v1/map``),
returning a list of discovered links (with titles where available). Useful for
finding pages on a site before fetching them with ``web_fetch``.

Configured in the agent spec::

    tools:
      builtins:
        - name: web_map
          api_key: ${NIMBLE_API_KEY}
          # optional:
          # limit: 50            # max links to return (default 50)

See https://docs.nimbleway.com/api-reference/map
"""

from __future__ import annotations

import json
import logging
import os

# Any: Nimble's JSON response is a heterogeneous dict with string keys and
# mixed value types (str, list, dict, bool, None).
from typing import Any

import httpx

from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)

_DEFAULT_NIMBLE_MAP_URL = "https://sdk.nimbleway.com/v1/map"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (same convention as the web_search / web_fetch backends).
_CLIENT_SOURCE = "omnigent"

# Default cap on returned links so a large site cannot blow the model context.
_DEFAULT_LIMIT = 50

# Nimble's Map endpoint crawls the site to discover URLs and can take ~100s even
# for a small site, so it needs a generous read timeout. The Nimble SDK's own
# ``map()`` exposes a per-request timeout override for the same reason.
_MAP_TIMEOUT_S = 180.0

_DESCRIPTION = (
    "Discover the URLs on a website — returns a list of links found by mapping "
    "the site. Use to find pages on a site before fetching them with web_fetch."
)


def _map_url() -> str:
    """Resolve the Nimble Map URL; ``OMNIGENT_NIMBLE_MAP_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_NIMBLE_MAP_URL", _DEFAULT_NIMBLE_MAP_URL)


def _resolve_limit(config: dict[str, str]) -> int:
    """
    Read ``limit`` from spec config, clamped to a sane 1-1000 range.

    Spec config values arrive as strings, so ``limit: 50`` reaches here as
    ``"50"``.

    :param config: Spec-level config; ``limit`` may be missing or a str.
    :returns: A valid link cap, or the default on missing/invalid input.
    """
    raw = config.get("limit")
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(value, 1000))


def _map_nimble(url: str, config: dict[str, str]) -> str:
    """
    Map a website's URLs via Nimble and format the results.

    :param url: The website URL to map.
    :param config: Spec-level config; ``api_key`` (required), optional ``limit``.
    :returns: A numbered list of discovered links, or an error message. Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_map config in config.yaml."
    try:
        resp = httpx.post(
            _map_url(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json={"url": url, "limit": _resolve_limit(config)},
            timeout=_MAP_TIMEOUT_S,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Nimble map error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Nimble map error: {exc}"
    return _format_map(resp.json())


def _format_map(data: dict[str, Any]) -> str:
    """
    Format Nimble's ``/v1/map`` response into a numbered link list.

    Nimble returns ``{"links": [{"url", "title"?, "description"?}], "success",
    "task_id"}``.

    :param data: The parsed JSON response from Nimble.
    :returns: A numbered list of ``url`` (with title when present), or a short message.
    """
    links = data.get("links") or []
    if not links:
        return "No links found."
    formatted: list[str] = []
    for i, link in enumerate(links):
        url = link.get("url", "")
        title = link.get("title") or ""
        entry = f"{i + 1}. {url}"
        if title:
            entry += f"\n   {title}"
        formatted.append(entry)
    return "\n".join(formatted)


class WebMapTool(Tool):
    """
    Website URL-discovery tool backed by Nimble's Map endpoint.

    :param config: Spec-level config, e.g. ``{"api_key": "...", "limit": "50"}``.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        :param config: Spec-level config with ``api_key`` and optional ``limit``.
        """
        self._config = config or {}

    @classmethod
    def name(cls) -> str:
        """:returns: ``"web_map"``."""
        return "web_map"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return _DESCRIPTION

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema for web_map.

        :returns: A function tool schema with a required ``url`` parameter.
        """
        return {
            "type": "function",
            "function": {
                "name": "web_map",
                "description": _DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The website URL to map.",
                        },
                    },
                    "required": ["url"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run web_map synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of this tool.
        :returns: ``False``.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a web_map call.

        :param arguments: JSON-encoded dict with a ``url`` key.
        :param ctx: Tool execution context (unused).
        :returns: A numbered link list, or an error message.
        """
        del ctx
        parsed: dict[str, Any] = json.loads(arguments)
        url = parsed.get("url")
        if not url:
            return "Error: 'url' parameter is required."
        return _map_nimble(str(url), self._config)
