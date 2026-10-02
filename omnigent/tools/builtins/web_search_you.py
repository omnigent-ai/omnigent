"""Built-in tool backend: You.com web search via its MCP endpoint.

Calls You.com's MCP server (``https://api.you.com/mcp``) with the
``you-search`` tool and returns a list of grounded results (title, URL,
description). Good for non-OpenAI models (Anthropic, Llama, Databricks-hosted,
etc.) that cannot use OpenAI's native ``web_search_preview``.

Like Keenable, You.com works **without an API key**: with no ``api_key``
in the spec it uses the keyless free profile (``?profile=free``), so it
runs out of the box. Supplying an ``api_key`` switches to the
authenticated endpoint (``Authorization: Bearer``) and lifts rate limits.

The backend speaks MCP streamable-HTTP directly — an ``initialize``
handshake followed by one ``tools/call`` — using only ``httpx``, so it
stays consistent with the other backends (no MCP client dependency).

Configured in the agent spec::

    tools:
      builtins:
        - name: web_search
          search_provider: you
          # api_key is optional — omit it to use the keyless free profile:
          # api_key: ${YOU_API_KEY}
          # max_results: 5            # 1-20 (default 5)

See https://you.com
"""

from __future__ import annotations

import json
import logging
import os

# Any: You.com's JSON payloads are heterogeneous dicts with string keys
# and mixed value types (str, list, dict, None).
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_YOU_MCP_URL = "https://api.you.com/mcp"

# Default number of results when the spec does not set ``max_results``.
# You.com's ``you-search`` accepts a ``count`` argument (1-100); we pass
# this value through and render that many results.
_DEFAULT_MAX_RESULTS: int = 5

# The MCP search tool we call and the protocol version we speak.
_SEARCH_TOOL_NAME = "you-search"
_MCP_PROTOCOL_VERSION = "2025-06-18"

# Identifies this client to the server in the initialize handshake.
_CLIENT_NAME = "Omnigent"


def _you_mcp_url() -> str:
    """Resolve the You.com MCP endpoint; ``OMNIGENT_YOU_MCP_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_YOU_MCP_URL", _DEFAULT_YOU_MCP_URL).rstrip("/")


def _resolve_max_results(config: dict[str, str]) -> int:
    """
    Read ``max_results`` from spec config, clamped to a 1-20 range.

    :param config: Spec-level config; ``max_results`` may be a str or int.
    :returns: A valid result count, or the default on missing/invalid input.
    """
    raw = config.get("max_results")
    if raw is None:
        return _DEFAULT_MAX_RESULTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RESULTS
    return max(1, min(value, 20))


def _result_objects(body: str) -> list[dict[str, Any]]:
    """
    Parse an MCP streamable-HTTP response body into its result objects.

    The body is either an SSE stream (``data: {...}`` lines; we keep the
    JSON-RPC objects that carry a ``result`` key, in order) or a plain
    JSON object.

    :param body: The raw response text from the MCP endpoint.
    :returns: JSON-RPC response objects containing a ``result`` field.
    """
    objects: list[dict[str, Any]] = []
    if body.lstrip().startswith("{"):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return objects
        if isinstance(parsed, dict) and "result" in parsed:
            objects.append(parsed)
        return objects
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            parsed = json.loads(line[len("data:") :].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "result" in parsed:
            objects.append(parsed)
    return objects


def _web_results(payload: Any) -> list[dict[str, Any]]:
    """
    Extract the web result list from a ``you-search`` tool result.

    ``tools/call`` wraps the tool output as ``{"content": [{"type": "text",
    "text": "<json>"}]}`` where ``<json>`` is ``{"results": {"web": [...]}}``.

    :param payload: The ``result`` object of a ``tools/call`` response.
    :returns: The web result dicts (empty when the payload has none).
    """
    if not isinstance(payload, dict):
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    text = next(
        (c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"),
        None,
    )
    if not isinstance(text, str) or not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        _logger.warning("you-search returned non-JSON tool output")
        return []
    results = parsed.get("results") if isinstance(parsed, dict) else None
    web = results.get("web") if isinstance(results, dict) else None
    return web if isinstance(web, list) else []


def _search_you(
    query: str,
    config: dict[str, str],
) -> str:
    """
    Call the You.com MCP endpoint and format the results.

    Keyless by default: with no ``api_key`` the free profile
    (``?profile=free``) is used. With an ``api_key`` the authenticated
    endpoint is used and the key is sent as a Bearer token.

    :param query: The search query string.
    :param config: Spec-level config; checks ``api_key`` and ``max_results``
        (both optional).
    :returns: Formatted results or an error message.
    """
    api_key = (config.get("api_key") or "").strip()
    base = _you_mcp_url()
    # Keyless free profile without a key; authenticated endpoint with one.
    url = base if api_key else f"{base}?profile=free"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        # MCP initialize handshake first. The server is currently stateless
        # per request, but doing the handshake keeps the exchange
        # spec-correct should that change.
        httpx.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": _CLIENT_NAME, "version": "0"},
                },
            },
            timeout=30.0,
        ).raise_for_status()
        resp = httpx.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": _SEARCH_TOOL_NAME,
                    "arguments": {
                        "query": query,
                        "count": _resolve_max_results(config),
                    },
                },
            },
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"You.com search error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"You.com search error: {exc}"

    results = _result_objects(resp.text)
    if not results:
        return "You.com search error: unexpected MCP response."
    web = _web_results(results[-1]["result"])
    return _format_results(web, _resolve_max_results(config))


#: Characters of description text kept per result. You.com returns a short
#: meta description per hit; this caps any outlier the same way the other
#: backends cap their snippets.
_SNIPPET_MAX_CHARS = 500


def _snippet(item: dict[str, Any]) -> str:
    """
    Return the description of one result, collapsed and capped.

    :param item: One entry of You.com's ``results.web`` list.
    :returns: The result's description, or an empty string when it has none.
    """
    text = str(item.get("description") or "")
    return " ".join(text.split())[:_SNIPPET_MAX_CHARS].strip()


def _format_results(results: list[dict[str, Any]], max_results: int) -> str:
    """
    Format You.com's web results into readable text.

    :param results: The ``results.web`` list from a ``you-search`` response.
    :param max_results: Maximum number of results to render.
    :returns: Numbered results, or a "no results" message.
    """
    if not results:
        return "No results found."

    formatted: list[str] = []
    for i, item in enumerate(results[:max_results]):
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url = item.get("url", "")
        snippet = _snippet(item)
        formatted.append(f"{i + 1}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(formatted) if formatted else "No results found."
