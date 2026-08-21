"""``web_read`` backend: Firecrawl single-URL retrieval.

Fetches one URL as clean markdown via Firecrawl's ``POST /v2/scrape``.
Firecrawl renders JavaScript and, with ``proxy: auto``, escalates to a more
capable proxy tier for higher reliability — an LLM-native alternative to Nimble
that is also self-hostable (open-source core).

Configured in the agent spec::

    tools:
      builtins:
        - name: web_read
          read_provider: firecrawl
          api_key: ${FIRECRAWL_API_KEY}
          # optional:
          # proxy: auto            # basic | enhanced | auto (default; escalates for reliability)

See https://docs.firecrawl.dev/api-reference/endpoint/scrape
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
# Proxy tier, escalating: ``basic`` (datacenter) -> ``enhanced`` (residential,
# higher reliability) -> ``auto`` (start basic, escalate to enhanced if needed).
_DEFAULT_PROXY = "auto"
_VALID_PROXIES = frozenset({"basic", "enhanced", "auto"})
_DEFAULT_TIMEOUT_S = 120.0


def _base_url() -> str:
    """Resolve the Firecrawl base URL; ``OMNIGENT_FIRECRAWL_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_FIRECRAWL_BASE_URL", _DEFAULT_BASE_URL)


def _read_firecrawl(url: str, config: dict[str, str]) -> str:
    """
    Fetch one URL as markdown via Firecrawl.

    :param url: The page URL to read (validated by the caller).
    :param config: Spec-level config; ``api_key`` (required), ``proxy`` (optional).
    :returns: Extracted markdown, or an error message (never raises).
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_read config in config.yaml."

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
                f"Firecrawl read error: HTTP {code} (auth/quota — check the api_key "
                "and plan in the web_read config)."
            )
        return f"Firecrawl read error: HTTP {code}"
    except httpx.RequestError as exc:
        # Covers connect/timeout/redirect/protocol/decoding errors uniformly.
        return f"Firecrawl read error: {exc}"

    try:
        payload = resp.json()
    except (ValueError, TypeError):
        return "Firecrawl read error: non-JSON response."
    return _format_read(payload, url)


def _format_read(payload: dict[str, Any], url: str) -> str:
    """
    Pull the markdown out of Firecrawl's ``/v2/scrape`` response.

    Firecrawl returns ``{"success": bool, "data": {"markdown": str, ...}}``.
    A response missing ``success`` is treated as a failure — the field is part
    of the documented contract, so its absence signals a malformed response.

    :param payload: The parsed JSON response.
    :param url: The requested URL (for the empty-result message).
    :returns: The markdown, or an error/empty message. Central truncation is
        applied by the dispatcher, not here.
    """
    if not isinstance(payload, dict) or not payload.get("success", False):
        return f"Firecrawl read error: {url} was not retrieved successfully."
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return f"web_read: no content extracted from {url} (page may be empty or blocked)."
    return markdown.strip()
