"""Built-in tool: Tavily web search.

Uses Tavily's agent-optimized search endpoint (``POST /search``) to
return a list of grounded results (title, URL, snippet) plus an
optional synthesized ``answer``. Good for non-OpenAI models
(Anthropic, Llama, Databricks-hosted, etc.) that cannot use OpenAI's
native ``web_search_preview``.

Configured in the agent spec::

    tools:
      builtins:
        - name: web_search
          search_provider: tavily
          api_key: ${TAVILY_API_KEY}
          # optional:
          # max_results: 5            # 1-20 (default 5)
          # search_depth: basic       # basic (default) or advanced

See https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

from __future__ import annotations

import logging
import os

# Any: Tavily's JSON response is a heterogeneous dict with string keys
# and mixed value types (str, float, list, dict, None).
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_TAVILY_URL = "https://api.tavily.com/search"

# Default number of results when the spec does not set ``max_results``.
# Tavily accepts 1-20; the API's own default is 5.
_DEFAULT_MAX_RESULTS: int = 5

# Supported search tiers. Non-default values are validated against this allowlist
# so a misconfigured spec gets a clear error rather than an opaque API failure.
_DEFAULT_SEARCH_DEPTH = "basic"
_VALID_SEARCH_DEPTHS = frozenset({"basic", "advanced"})

# Identifies this integration to Tavily via the ``X-Client-Source`` header so
# traffic from the Omnigent provider is attributable.
_CLIENT_SOURCE = "omnigent"


def _tavily_url() -> str:
    """Resolve the Tavily Search URL; ``OMNIGENT_TAVILY_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_TAVILY_BASE_URL", _DEFAULT_TAVILY_URL)


def _resolve_max_results(config: dict[str, str]) -> int:
    """
    Read ``max_results`` from spec config, clamped to Tavily's 1-20 range.

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


def _search_tavily(
    query: str,
    config: dict[str, str],
) -> str:
    """
    Call the Tavily web search API and format the results.

    :param query: The search query string.
    :param config: Spec-level config; checked for ``api_key`` (required),
        ``max_results`` and ``search_depth`` (optional).
    :returns: Formatted results or an error message.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_search config in config.yaml."
    search_depth = config.get("search_depth", _DEFAULT_SEARCH_DEPTH)
    if search_depth not in _VALID_SEARCH_DEPTHS:
        return (
            f"Error: unsupported search_depth {search_depth!r}. "
            f"Use one of: {', '.join(sorted(_VALID_SEARCH_DEPTHS))}."
        )
    try:
        resp = httpx.post(
            _tavily_url(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json={
                "query": query,
                "max_results": _resolve_max_results(config),
                "search_depth": search_depth,
                "include_answer": True,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Tavily search error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Tavily search error: {exc}"

    return _format_results(resp.json())


def _format_results(data: dict[str, Any]) -> str:
    """
    Format Tavily's ``/search`` JSON response into readable text.

    Tavily returns ``{"results": [{"title", "url", "content", ...}],
    "answer": str | None, ...}``. If the response includes a non-null
    ``answer``, it is shown first.

    :param data: The parsed JSON response from Tavily.
    :returns: An optional answer followed by numbered results.
    """
    results = data.get("results", [])
    answer = data.get("answer")
    if not results:
        # Don't discard an answer just because the result list is empty.
        return answer or "No results found."

    formatted: list[str] = []
    for i, item in enumerate(results):
        title = item.get("title", "")
        url = item.get("url", "")
        snippet = item.get("content") or ""
        formatted.append(f"{i + 1}. {title}\n   {url}\n   {snippet}")
    body = "\n\n".join(formatted)

    if answer:
        return f"{answer}\n\n{body}"
    return body
