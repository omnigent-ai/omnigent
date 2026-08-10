"""``web_scrape`` backend: Jina Reader single-URL extraction.

Fetches one URL as clean markdown via Jina Reader (``https://r.jina.ai/<url>``).
Jina renders JavaScript and returns LLM-ready markdown. It works **keyless**
(rate-limited); an ``api_key`` lifts the rate limit and enables higher-tier
features. Lightweight and cheap — a good default for public, lightly-protected
pages. It does not actively fight aggressive anti-bot (Cloudflare/DataDome); for
those, use the ``nimble`` or ``firecrawl`` backends.

Configured in the agent spec::

    tools:
      builtins:
        - name: web_scrape
          scrape_provider: jina
          # api_key: ${JINA_API_KEY}   # optional — keyless works, keyed lifts rate limits

See https://jina.ai/reader
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

_DEFAULT_BASE_URL = "https://r.jina.ai"
_DEFAULT_TIMEOUT_S = 90.0


def _base_url() -> str:
    """Resolve the Jina Reader base URL; ``OMNIGENT_JINA_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_JINA_BASE_URL", _DEFAULT_BASE_URL)


def _scrape_jina(url: str, config: dict[str, str]) -> str:
    """
    Fetch one URL as markdown via Jina Reader.

    Keyless by default: unlike the other backends, ``api_key`` is optional and
    only raises the rate limit when present.

    :param url: The page URL to scrape (validated by the caller).
    :param config: Spec-level config; ``api_key`` optional.
    :returns: Extracted markdown, or an error message (never raises).
    """
    headers = {
        # Ask Reader for markdown (its default) rather than the JSON envelope.
        "Accept": "text/markdown",
    }
    api_key = config.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Append the target URL to Reader's canonical form
    # ("https://r.jina.ai/https://site/page"), but percent-encode the parts that
    # would otherwise bind to r.jina.ai instead of the page: a raw "?"/"&"/"#"
    # makes "https://r.jina.ai/https://site?q=x" attach "?q=x" to the Reader
    # request, not the scraped page. Keeping ":" and "/" literal preserves the
    # documented scheme+path form so this works whether or not Reader re-decodes.
    target = f"{_base_url()}/{quote(url, safe=':/')}"

    try:
        resp = httpx.get(
            target,
            headers=headers,
            timeout=_DEFAULT_TIMEOUT_S,
            follow_redirects=True,
        )
        resp.raise_for_status()
        content = (resp.text or "").strip()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403, 429):
            return (
                f"Jina scrape error: HTTP {code} (rate/tier limit). Set an api_key in "
                "the web_scrape config to raise the limit."
            )
        return f"Jina scrape error: HTTP {code}"
    except httpx.RequestError as exc:
        # Covers connect/timeout/redirect/protocol/decoding errors uniformly.
        return f"Jina scrape error: {exc}"

    if not content:
        return f"web_scrape: no content extracted from {url} (page may be empty or blocked)."
    return content
