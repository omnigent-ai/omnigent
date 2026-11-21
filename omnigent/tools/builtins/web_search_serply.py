"""Built-in tool: Serply web search.

Uses Serply's Google search API (``GET /v1/search/``) to return a list of
grounded results (title, URL, snippet). The same key also serves Google News
(``GET /v1/news/``) and Google Scholar (``GET /v1/scholar/``), selected with
the optional ``search_type`` setting. Good for non-OpenAI models (Llama, Mistral,
Databricks-hosted, etc.) that cannot use OpenAI's native
``web_search_preview``.

Configured in the agent spec::

    tools:
      builtins:
        - name: web_search
          search_provider: serply
          api_key: ${SERPLY_API_KEY}
          # optional:
          # max_results: 5            # 1-10 (default 5)
          # search_type: web          # web (default), news, or scholar

See https://serply.io/docs (API reference) and https://serply.io.
"""

from __future__ import annotations

import html
import logging
import os
import re

# Any: Serply's JSON response is a heterogeneous dict with string keys
# and mixed value types (str, int, list, dict, None).
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_SERPLY_URL = "https://api.serply.io/v1"

# Default number of results when the spec does not set ``max_results``.
# One Serply page holds at most 10 results; larger values are ignored server-side.
_DEFAULT_MAX_RESULTS: int = 5
_MAX_PAGE_SIZE: int = 10

# Supported search verticals, mapped to their API path segment. Non-default
# values are validated against this allowlist so a misconfigured spec gets a
# clear error rather than an opaque API failure.
_DEFAULT_SEARCH_TYPE = "web"
_ENDPOINTS: dict[str, str] = {
    "web": "search",
    "news": "news",
    "scholar": "scholar",
}

# Identifies this integration to Serply via the ``User-Agent`` header so
# traffic from the Omnigent provider is attributable.
_CLIENT_SOURCE = "omnigent-web-search"

_TAG_RE = re.compile(r"<[^>]+>")


def _serply_base_url() -> str:
    """Resolve the Serply base URL; ``OMNIGENT_SERPLY_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_SERPLY_BASE_URL", _DEFAULT_SERPLY_URL).rstrip("/")


def _resolve_max_results(config: dict[str, str]) -> int:
    """
    Read ``max_results`` from spec config, clamped to Serply's 1-10 page range.

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
    return max(1, min(value, _MAX_PAGE_SIZE))


def _search_serply(
    query: str,
    config: dict[str, str],
) -> str:
    """
    Call the Serply search API and format the results.

    :param query: The search query string.
    :param config: Spec-level config; checked for ``api_key`` (required),
        ``search_type`` and ``max_results`` (optional).
    :returns: Formatted results or an error message.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_search config in config.yaml."
    search_type = config.get("search_type", _DEFAULT_SEARCH_TYPE)
    endpoint = _ENDPOINTS.get(search_type)
    if endpoint is None:
        return (
            f"Error: unsupported search_type {search_type!r}. "
            f"Use one of: {', '.join(sorted(_ENDPOINTS))}."
        )
    max_results = _resolve_max_results(config)
    try:
        resp = httpx.get(
            f"{_serply_base_url()}/{endpoint}/",
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "User-Agent": _CLIENT_SOURCE,
            },
            params={"q": query, "num": max_results},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Serply search error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Serply search error: {exc}"

    return _format_results(resp.json(), search_type, max_results)


def _clean_text(value: object) -> str:
    """Strip HTML tags and entities from a snippet and collapse whitespace."""
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(_TAG_RE.sub("", value)).split())


def _format_results(data: dict[str, Any], search_type: str, max_results: int) -> str:
    """
    Format a Serply JSON response into readable text.

    Web search returns ``{"results": [{"title", "link", "description", ...}]}``,
    news returns ``{"entries": [{"title", "link", "summary", "source", ...}]}``
    (RSS-shaped, ``summary`` is HTML and ``num`` is ignored server-side), and
    scholar returns ``{"articles": [{"title", "link", "description", "extras",
    ...}]}``. The list is sliced to ``max_results`` and rendered as numbered
    entries.

    :param data: The parsed JSON response from Serply.
    :param search_type: One of the ``_ENDPOINTS`` keys; picks the result list.
    :param max_results: Maximum number of results to render.
    :returns: Numbered results, or a "no results" message.
    """
    list_key = {"web": "results", "news": "entries", "scholar": "articles"}[search_type]
    results = data.get(list_key) if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return "No results found."

    formatted: list[str] = []
    for i, item in enumerate(results[:max_results]):
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"))
        url = item.get("link") or ""
        if search_type == "news":
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            snippet = _clean_text(item.get("summary")) or _clean_text(source.get("title"))
        else:
            snippet = _clean_text(item.get("description"))
            if search_type == "scholar":
                extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
                citations = extras.get("citations") if isinstance(extras, dict) else None
                count = citations.get("count") if isinstance(citations, dict) else None
                if isinstance(count, int) and count > 0:
                    snippet = f"{snippet} (cited by {count})".strip()
        formatted.append(f"{i + 1}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(formatted) if formatted else "No results found."
