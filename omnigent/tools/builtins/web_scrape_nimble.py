"""``web_scrape`` backend: Nimble Web API single-URL extraction.

Fetches one arbitrary URL as clean content via Nimble's Web API
(``POST /v2/extract``), which drives a real browser with anti-bot evasion —
so bot-protected / JavaScript-rendered pages return usable content where a
plain HTTP fetch gets a 403 or an empty skeleton.

Distinct from the ``nimble_extract`` builtin (which runs a pre-defined extract
*template* against known sites) and ``web_search`` (which returns result links):
this is "give me any URL, get the page back."

Configured in the agent spec::

    tools:
      builtins:
        - name: web_scrape
          scrape_provider: nimble
          api_key: ${NIMBLE_API_KEY}
          # optional:
          # driver: vx8            # vx6 (plain HTTP) | vx8 (JS + anti-bot) | vx10 (hardest)
          # output_format: markdown  # markdown (default) | html

See https://docs.nimbleway.com/api-reference/web-api/extract
"""

from __future__ import annotations

import logging
import os

# Any: Nimble's JSON response is a heterogeneous dict with mixed value types.
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://sdk.nimbleway.com"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (same convention as the other Nimble builtins).
_CLIENT_SOURCE = "omnigent"

# Collection driver / tier, weakest -> strongest. The plain HTTP tier (vx6) is
# fast but has no JS render or anti-bot evasion, so protected sites block it;
# vx8 adds both and is the right default for "get past the robot filter". vx10
# is for the hardest targets. Validated against this allowlist so a typo yields
# a clear error rather than an opaque API failure.
_DEFAULT_DRIVER = "vx8"
_VALID_DRIVERS = frozenset({"vx6", "vx8", "vx10"})

_DEFAULT_OUTPUT_FORMAT = "markdown"
_VALID_OUTPUT_FORMATS = frozenset({"markdown", "html"})

# A browser-driven extract can be slow; allow a generous read timeout.
_DEFAULT_TIMEOUT_S = 120.0

# Cap the returned content so a large page cannot blow the model context.
_MAX_CONTENT_CHARS = 50_000

# Substrings that indicate the target site (not Nimble) blocked the fetch, so
# the tool can say "blocked" rather than returning a captcha/denied page as if
# it were content. Mirrors the ferguson Nimble PoC's block-marker set.
_BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "are you a robot",
    "verify you are human",
    "unusual traffic",
    "request unsuccessful",
    "cloudflare",
    "attention required",
)


def _base_url() -> str:
    """Resolve the Nimble base URL; ``OMNIGENT_NIMBLE_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_NIMBLE_BASE_URL", _DEFAULT_BASE_URL)


def _resolve_driver(config: dict[str, str]) -> str | None:
    """Return the configured driver, or ``None`` if it is invalid.

    :param config: Spec-level config; ``driver`` optional.
    :returns: A valid driver name, or ``None`` for an unrecognized value.
    """
    driver = config.get("driver", _DEFAULT_DRIVER)
    return driver if driver in _VALID_DRIVERS else None


def _scrape_nimble(url: str, config: dict[str, str]) -> str:
    """
    Fetch one URL as clean content via Nimble's Web API.

    :param url: The page URL to scrape (validated by the caller).
    :param config: Spec-level config; ``api_key`` (required), ``driver`` and
        ``output_format`` (optional).
    :returns: Extracted content, or an error message (never raises).
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_scrape config in config.yaml."

    driver = _resolve_driver(config)
    if driver is None:
        return (
            f"Error: unsupported driver {config.get('driver')!r}. "
            f"Use one of: {', '.join(sorted(_VALID_DRIVERS))}."
        )

    output_format = config.get("output_format", _DEFAULT_OUTPUT_FORMAT)
    if output_format not in _VALID_OUTPUT_FORMATS:
        return (
            f"Error: unsupported output_format {output_format!r}. "
            f"Use one of: {', '.join(sorted(_VALID_OUTPUT_FORMATS))}."
        )

    try:
        resp = httpx.post(
            f"{_base_url()}/v2/extract",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json={
                "url": url,
                # ``render`` turns on the browser; ``vx8``/``vx10`` add anti-bot.
                "render": driver != "vx6",
                "driver": driver,
                "output_format": output_format,
            },
            timeout=_DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return (
                f"Nimble scrape error: HTTP {code} (authentication failed — check the "
                "api_key in the web_scrape config)."
            )
        return f"Nimble scrape error: HTTP {code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Nimble scrape error: {exc}"

    try:
        payload = resp.json()
    except (ValueError, TypeError):
        return "Nimble scrape error: the extract returned a non-JSON response."
    if not isinstance(payload, dict):
        return "Nimble scrape error: the extract returned an unexpected response shape."
    return _format_scrape(payload, url, driver)


def _format_scrape(payload: dict[str, Any], url: str, driver: str) -> str:
    """
    Pull the extracted content out of Nimble's ``/v2/extract`` response.

    Nimble nests the page body under ``parsing``/``content`` (or a top-level
    ``content``/``html_content``) depending on tier; we take the first
    non-empty one. A body that is only a block marker (captcha/denied) is
    reported as blocked so the model doesn't treat it as real content.

    :param payload: The parsed JSON response.
    :param url: The requested URL (for the blocked-message text).
    :param driver: The driver used (named in the blocked message so the caller
        can retry a stronger tier).
    :returns: The content (capped), or a blocked/empty message.
    """
    parsing = payload.get("parsing") if isinstance(payload.get("parsing"), dict) else {}
    content = (
        parsing.get("content")
        or payload.get("content")
        or payload.get("html_content")
        or payload.get("markdown")
        or ""
    )
    if not isinstance(content, str):
        content = str(content)
    content = content.strip()

    if not content:
        return (
            f"web_scrape: no content extracted from {url} (the page may be empty or "
            f"blocked on driver {driver!r}; try a stronger driver such as vx10)."
        )

    lowered = content.lower()
    if len(content) < 500 and any(marker in lowered for marker in _BLOCK_MARKERS):
        return (
            f"web_scrape: {url} appears to be bot-protected — the fetch returned a "
            f"challenge/denied page on driver {driver!r}. Try a stronger driver "
            "(vx10) or a different scrape_provider."
        )

    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS] + "\n\n[content truncated]"
    return content
