"""``web_read`` backend: Nimble Web API single-URL retrieval.

Fetches one arbitrary URL as clean content via Nimble's Web API
(``POST /v2/extract``), which drives a real browser to render JavaScript — so
client-rendered pages return usable content where a plain HTTP fetch returns an
empty skeleton or an error response.

Distinct from the ``nimble_extract`` builtin (which runs a pre-defined extract
*template* against known sites) and ``web_search`` (which returns result links):
this is "give me any URL, get the page back."

Configured in the agent spec::

    tools:
      builtins:
        - name: web_read
          read_provider: nimble
          api_key: ${NIMBLE_API_KEY}
          # optional:
          # driver: vx8            # auto | vx8 default | vx10 / vx12 (+ -pro) | vx6 (plain HTTP)
          # output_format: markdown  # markdown (default) | html

See https://docs.nimbleway.com/nimble-sdk/web-tools/extract/quickstart
"""

from __future__ import annotations

import os

# Any: Nimble's JSON response is a heterogeneous dict with mixed value types.
from typing import Any

import httpx

_DEFAULT_BASE_URL = "https://sdk.nimbleway.com"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (same convention as the other Nimble builtins).
_CLIENT_SOURCE = "omnigent"

# Collection driver / tier — the full documented enum from Nimble's Extract
# ``ExtractPayload`` schema. Validated against this allowlist so a typo yields a
# clear error rather than an opaque API failure. ``vx8`` is the default: it
# renders JavaScript and suits most pages; higher tiers (``vx10``/``vx12`` and
# their ``-pro`` variants) trade cost for reliability, and ``auto`` lets Nimble
# pick and escalate per domain. The HTTP-only tiers below do not render JS.
_DEFAULT_DRIVER = "vx8"
_VALID_DRIVERS = frozenset(
    {
        "auto",
        "vx6",
        "vx8",
        "vx8-pro",
        "vx10",
        "vx10-pro",
        "vx12",
        "vx12-pro",
        "media-vx6",
        "fast-vx6",
    }
)
# Drivers that do NOT execute JavaScript (plain HTTP). For these, ``render`` is
# false; ``auto`` sends ``render: "auto"`` (Nimble decides); all others render.
_HTTP_ONLY_DRIVERS = frozenset({"vx6", "media-vx6", "fast-vx6"})

_DEFAULT_OUTPUT_FORMAT = "markdown"
_VALID_OUTPUT_FORMATS = frozenset({"markdown", "html"})

# A browser-driven extract can be slow; allow a generous read timeout. Jina is
# lighter (no full browser), so its backend uses a shorter timeout.
_DEFAULT_TIMEOUT_S = 120.0

# Substrings that indicate the target site returned a challenge/denied response
# instead of the page, so the tool can report that rather than handing the
# model a challenge page as if it were real content.
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


def _render_for(driver: str) -> bool | str:
    """Map a driver to the matching ``render`` value for the request body.

    :param driver: A validated driver name.
    :returns: ``"auto"`` for the ``auto`` driver (Nimble decides and escalates),
        ``False`` for the HTTP-only tiers (no JavaScript), ``True`` otherwise.
    """
    if driver == "auto":
        return "auto"
    return driver not in _HTTP_ONLY_DRIVERS


def _read_nimble(url: str, config: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Fetch one URL as clean content via Nimble's Web API.

    :param url: The page URL to read (validated by the caller).
    :param config: Spec-level config; ``api_key`` (required), ``driver`` and
        ``output_format`` (optional).
    :returns: ``(content, diagnostic)`` — the extracted content on success, or a
        diagnostic message on failure / empty / blocked page. Exactly one is
        non-None. Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return None, "Error: api_key must be provided in the web_read config in config.yaml."

    driver = _resolve_driver(config)
    if driver is None:
        return None, (
            f"Error: unsupported driver {config.get('driver')!r}. "
            f"Use one of: {', '.join(sorted(_VALID_DRIVERS))}."
        )

    output_format = config.get("output_format", _DEFAULT_OUTPUT_FORMAT)
    if output_format not in _VALID_OUTPUT_FORMATS:
        return None, (
            f"Error: unsupported output_format {output_format!r}. "
            f"Use one of: {', '.join(sorted(_VALID_OUTPUT_FORMATS))}."
        )

    # ``formats`` is a list in preference order; we request the single chosen
    # format. For markdown, ``markdown_backend: main_content`` runs Mozilla
    # Readability first so boilerplate (nav/ads) is stripped before conversion.
    body: dict[str, Any] = {
        "url": url,
        "formats": [output_format],
        # ``render`` turns on the browser (JS execution). The HTTP-only tiers
        # don't render; ``auto`` lets Nimble decide; every other tier renders.
        "render": _render_for(driver),
        "driver": driver,
    }
    if output_format == "markdown":
        body["markdown_backend"] = "main_content"

    try:
        resp = httpx.post(
            f"{_base_url()}/v2/extract",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json=body,
            timeout=_DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return None, (
                f"Nimble read error: HTTP {code} (authentication failed — check the "
                "api_key in the web_read config)."
            )
        return None, f"Nimble read error: HTTP {code}"
    except httpx.RequestError as exc:
        # Covers connect/timeout/redirect/protocol/decoding errors uniformly.
        return None, f"Nimble read error: {exc}"

    try:
        payload = resp.json()
    except (ValueError, TypeError):
        return None, "Nimble read error: the extract returned a non-JSON response."
    if not isinstance(payload, dict):
        return None, "Nimble read error: the extract returned an unexpected response shape."
    return _format_read(payload, url, driver, output_format)


def _format_read(
    payload: dict[str, Any], url: str, driver: str, output_format: str
) -> tuple[str | None, str | None]:
    """
    Pull the extracted content out of Nimble's ``/v2/extract`` response.

    The page body lives under ``data`` as ``data.markdown`` / ``data.html``. We
    read the field matching the format we requested, falling back to the other
    if it is absent, using "first present" (not "first truthy") so a
    legitimately empty page doesn't get masked. A body that is only a block
    marker (captcha/denied) is reported as a challenge response so the model
    doesn't treat it as real content.

    :param payload: The parsed JSON response.
    :param url: The requested URL (for the blocked-message text).
    :param driver: The driver used (named in the blocked message so the caller
        can retry a stronger tier).
    :param output_format: The requested format (``markdown`` or ``html``), read
        preferentially from the response.
    :returns: ``(content, diagnostic)`` — exactly one is non-None. Central
        truncation is applied by the dispatcher, not here.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # Prefer the format we asked for; fall back to the other if it's missing.
    # "First present" (not None), so an empty string (empty page) isn't skipped.
    other = "html" if output_format == "markdown" else "markdown"
    raw: object = ""
    for candidate in (data.get(output_format), data.get(other)):
        if candidate is not None:
            raw = candidate
            break
    content = raw if isinstance(raw, str) else str(raw)
    content = content.strip()

    if not content:
        return None, (
            f"web_read: no content extracted from {url} (the page may be empty or "
            f"blocked on driver {driver!r}; try a higher tier such as vx10 or vx12)."
        )

    lowered = content.lower()
    if len(content) < 500 and any(marker in lowered for marker in _BLOCK_MARKERS):
        return None, (
            f"web_read: {url} returned a challenge/denied response instead of the "
            f"page on driver {driver!r}. Try a higher tier (vx10 / vx12, or their "
            "-pro variants) or a different read_provider."
        )

    return content, None
