"""Nimble Extract-backed ``web_fetch``.

Backs the ``web_fetch`` builtin when the agent spec selects
``fetch_provider: nimble``. Uses Nimble's Extract endpoint
(``POST /v1/extract``) to fetch a single URL with JavaScript rendering,
anti-bot handling, and optional geo-targeting, returning the page as
Markdown — a real upgrade over the default researcher sub-agent's
``curl`` / DuckDuckGo path when the goal is to read a specific URL.

Configured in the agent spec::

    tools:
      builtins:
        - name: web_fetch
          fetch_provider: nimble
          api_key: ${NIMBLE_API_KEY}
          # optional:
          # render: true        # JavaScript rendering (default: true)
          # country: US          # geo-targeting / proxy country

Without ``fetch_provider: nimble`` the builtin keeps its existing
behaviour (a research sub-agent). See
``omnigent/runner/tool_dispatch.py::_execute_web_fetch_tool`` for the
dispatch branch.

See https://docs.nimbleway.com/api-reference/extract
"""

from __future__ import annotations

import logging
import os

# Any: Nimble's JSON response is a heterogeneous dict with string keys
# and mixed value types (str, dict, list, None).
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_NIMBLE_EXTRACT_URL = "https://sdk.nimbleway.com/v1/extract"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (e.g. langchain-nimble sends ``"langchain-nimble"``),
# so traffic from the Omnigent provider is attributable. Same convention as
# ``web_search_nimble``.
_CLIENT_SOURCE = "omnigent"

# Cap the returned content so a very large page cannot blow the model's
# context window. Extract returns full page content; the model only needs
# enough to answer.
_MAX_CONTENT_CHARS = 50_000


def _extract_url() -> str:
    """Resolve the Nimble Extract URL; ``OMNIGENT_NIMBLE_EXTRACT_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_NIMBLE_EXTRACT_URL", _DEFAULT_NIMBLE_EXTRACT_URL)


def _resolve_render(config: dict[str, str]) -> bool:
    """
    Parse the optional ``render`` flag from spec config.

    Spec config values arrive as strings (the parser coerces every value to
    ``str``), so ``render: true`` in YAML reaches here as ``"true"``. Defaults
    to ``True`` — JavaScript rendering is the point of using Extract.

    :param config: Spec-level config; ``render`` may be missing or a str.
    :returns: Whether to render JavaScript.
    """
    raw = config.get("render")
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("false", "0", "no", "off")


def _fetch_nimble(url: str, config: dict[str, str]) -> str:
    """
    Fetch a URL via Nimble Extract and return its content as text.

    :param url: The target URL to fetch.
    :param config: Spec-level config; ``api_key`` (required), optional
        ``render`` (JavaScript rendering, default on) and ``country``
        (geo-targeting / proxy country).
    :returns: The page content (Markdown preferred, HTML fallback) prefixed
        with the final URL, or an error message. Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the web_fetch config in config.yaml."

    body: dict[str, Any] = {
        "url": url,
        "render": _resolve_render(config),
        "formats": ["markdown"],
    }
    country = config.get("country")
    if country:
        body["country"] = country

    try:
        resp = httpx.post(
            _extract_url(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json=body,
            timeout=60.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Nimble extract error: HTTP {exc.response.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return f"Nimble extract error: {exc}"

    return _format_extract(resp.json(), url)


def _format_extract(data: dict[str, Any], requested_url: str) -> str:
    """
    Format Nimble's ``/v1/extract`` response into readable text for the LLM.

    Nimble returns ``{"data": {"markdown": str | None, "html": str | None,
    ...}, "url": str, "status": str, ...}``. Prefer ``markdown`` (LLM-native),
    fall back to ``html``. Truncate to keep the model context bounded.

    :param data: The parsed JSON response from Nimble.
    :param requested_url: The URL originally requested (fallback if the
        response omits the final URL).
    :returns: The page content prefixed with its source URL, or a short
        status message when no content was extracted.
    """
    content = data.get("data") or {}
    body = content.get("markdown") or content.get("html") or ""
    final_url = data.get("url") or requested_url

    if not body.strip():
        status = data.get("status", "unknown")
        return f"No content extracted from {final_url} (status: {status})."

    if len(body) > _MAX_CONTENT_CHARS:
        body = body[:_MAX_CONTENT_CHARS] + "\n\n[content truncated]"

    return f"Source: {final_url}\n\n{body}"
