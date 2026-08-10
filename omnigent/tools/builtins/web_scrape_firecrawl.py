"""``web_scrape`` backend: Firecrawl single-URL scrape.

Fetches one URL as clean markdown via Firecrawl's ``POST /v2/scrape``.
Firecrawl renders JavaScript and, with ``proxy: auto``, retries through a
stealth proxy when a basic fetch is blocked — an LLM-native alternative to
Nimble that is also self-hostable (open-source core).

Configured in the agent spec::

    tools:
      builtins:
        - name: web_scrape
          scrape_provider: firecrawl
          api_key: ${FIRECRAWL_API_KEY}
          # optional:
          # proxy: auto            # basic | stealth | auto (default; escalates on block)

See https://docs.firecrawl.dev/api-reference/endpoint/scrape
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
_DEFAULT_PROXY = "auto"
_VALID_PROXIES = frozenset({"basic", "stealth", "auto"})
_DEFAULT_TIMEOUT_S = 120.0
_MAX_CONTENT_CHARS = 50_000


def _base_url() -> str:
    """Resolve the Firecrawl base URL; ``OMNIGENT_FIRECRAWL_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_FIRECRAWL_BASE_URL", _DEFAULT_BASE_URL)


def _scrape_firecrawl(url: str, config: dict[str, str]) -> str:
    """
    Fetch one URL as markdown via Firecrawl.

    :param url: The page URL to scrape (validated by the caller).
    :param config: Spec-level config; ``api_key`` (required), ``proxy`` (optional).
    :returns: Extracted markdown, or an error message (never raises).
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_scrape config in config.yaml."

    proxy = config.get("proxy", _DEFAULT_PROXY)
    if proxy not in _VALID_PROXIES:
        return (
            f"Error: unsupported proxy {proxy!r}. Use one of: {', '.join(sorted(_VALID_PROXIES))}."
        )

    try:
        resp = httpx.post(
            f"{_base_url()}/v2/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"], "proxy": proxy},
            timeout=_DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 402, 403):
            return (
                f"Firecrawl scrape error: HTTP {code} (auth/quota — check the api_key "
                "and plan in the web_scrape config)."
            )
        return f"Firecrawl scrape error: HTTP {code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Firecrawl scrape error: {exc}"

    try:
        payload = resp.json()
    except (ValueError, TypeError):
        return "Firecrawl scrape error: non-JSON response."
    return _format_scrape(payload, url)


def _format_scrape(payload: dict[str, Any], url: str) -> str:
    """
    Pull the markdown out of Firecrawl's ``/v2/scrape`` response.

    Firecrawl returns ``{"success": bool, "data": {"markdown": str, ...}}``.

    :param payload: The parsed JSON response.
    :param url: The requested URL (for the empty-result message).
    :returns: The markdown (capped), or an error/empty message.
    """
    if not isinstance(payload, dict) or not payload.get("success", True):
        return f"Firecrawl scrape error: {url} was not scraped successfully."
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    markdown = data.get("markdown") or ""
    if not isinstance(markdown, str) or not markdown.strip():
        return f"web_scrape: no content extracted from {url} (page may be empty or blocked)."
    markdown = markdown.strip()
    if len(markdown) > _MAX_CONTENT_CHARS:
        markdown = markdown[:_MAX_CONTENT_CHARS] + "\n\n[content truncated]"
    return markdown
