"""Built-in tool: Keenable web search.

Uses Keenable's agent-optimized search endpoint (``POST /v1/search``) to
return a list of grounded results (title, URL, description). Good for
non-OpenAI models (Anthropic, Llama, Databricks-hosted, etc.) that cannot
use OpenAI's native ``web_search_preview``.

Unlike the other backends, Keenable works **without an API key**: with no
``api_key`` in the spec it calls the keyless public endpoint
(``/v1/search/public``), so it runs out of the box. Supplying an ``api_key``
switches to the authenticated endpoint (``/v1/search``) and lifts rate limits.

Configured in the agent spec::

    tools:
      builtins:
        - name: web_search
          search_provider: keenable
          # api_key is optional — omit it to use the keyless free tier:
          # api_key: ${KEENABLE_API_KEY}
          # max_results: 5            # 1-20 (default 5)

See https://docs.keenable.ai
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_KEENABLE_URL = "https://api.keenable.ai"
_DEFAULT_MAX_RESULTS: int = 5
_CLIENT_TITLE = "Omnigent"


def _keenable_base_url() -> str:
    """Resolve the Keenable base URL; ``OMNIGENT_KEENABLE_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_KEENABLE_BASE_URL", _DEFAULT_KEENABLE_URL).rstrip("/")


def _resolve_max_results(config: dict[str, str]) -> int:
    """Read and clamp ``max_results`` to the supported 1-20 range."""
    raw = config.get("max_results")
    if raw is None:
        return _DEFAULT_MAX_RESULTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RESULTS
    return max(1, min(value, 20))


def _search_keenable(query: str, config: dict[str, str]) -> str:
    """Call Keenable and return formatted results or a readable error."""
    api_key = (config.get("api_key") or "").strip()
    path = "/v1/search" if api_key else "/v1/search/public"
    headers = {
        "Content-Type": "application/json",
        "X-Keenable-Title": _CLIENT_TITLE,
    }
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        resp = httpx.post(
            f"{_keenable_base_url()}{path}",
            headers=headers,
            json={"query": query, "mode": "pro"},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Keenable search error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Keenable search error: {exc}"

    return _format_results(resp.json(), _resolve_max_results(config))


def _format_results(data: dict[str, Any], max_results: int) -> str:
    """Format Keenable results into readable numbered title/URL/text blocks.

    Keenable exposes the useful page text as ``snippet``. Some responses also
    provide ``description``; prefer it when non-empty for backwards
    compatibility, then fall back to ``snippet`` so empty descriptions do not
    discard the result text.
    """
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return "No results found."

    formatted: list[str] = []
    for i, item in enumerate(results[:max_results]):
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url = item.get("url", "")
        snippet = item.get("description") or item.get("snippet") or ""
        formatted.append(f"{i + 1}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(formatted) if formatted else "No results found."
