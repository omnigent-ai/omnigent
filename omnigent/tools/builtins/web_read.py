"""Built-in tool: unified web read (single-URL page reader).

Fetches the content of one URL as clean text/markdown through a managed
retrieval backend that renders JavaScript — so an agent can read pages that a
plain HTTP fetch (``web_fetch``) returns empty or an error for (e.g. a
client-rendered page, or a site that refuses a bare request). Complements
``web_search`` (which returns result links, not page content).

The ``read_provider`` key in the spec selects the backend from the
``_BACKENDS`` registry; there is no default and no ``api_key`` env-var fallback,
so the spec is self-contained and the engine used is explicit. (A backend may
read an ``OMNIGENT_*_BASE_URL`` override, used only to point tests at a mock.)

Usage in config.yaml::

    tools:
      builtins:
        - name: web_read
          read_provider: nimble      # or firecrawl / jina
          api_key: ${NIMBLE_API_KEY}   # keyed backends need it; jina is keyless
          # driver: vx8                # nimble only (auto/vx8/vx10/vx12, + -pro; vx6 plain HTTP)
          # output_format: markdown    # nimble only (markdown/html)
          # proxy: auto                # firecrawl only (basic/enhanced/auto)

Responsible use:
    This tool performs a single, user-initiated fetch of one URL — it is not a
    crawler. It is intended for public, non-authenticated pages, and it does not
    access logins or paywalls: it reads a page as an unauthenticated visitor
    would. Fetching is delegated to the configured provider, which is expected
    to honor the site's ``robots.txt``. You remain responsible for using it in
    line with the target site's Terms of Service and applicable law. This is
    general guidance, not legal advice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

# Cap returned content so one large page cannot dominate the model context.
# Applied centrally here so every backend truncates identically; the runtime
# also enforces a much larger global tool-output cap as a backstop.
_MAX_CONTENT_CHARS = 50_000


@dataclass(frozen=True)
class _Backend:
    """A selectable ``read_provider`` engine in the ``_BACKENDS`` registry.

    :param run: Callable ``(url, config) -> (content, diagnostic)``. Exactly one
        of the two is non-``None``: a successful read returns
        ``(page_markdown, None)``; any failure or non-content outcome (missing
        key, HTTP error, empty/blocked page) returns ``(None, message)``. This
        explicit split is what lets the dispatcher tell content from a
        diagnostic without inspecting the string.
    :param keyless: True if the engine needs no ``api_key`` (drives hint text).
    :param options: Optional config keys this engine reads *beyond* the shared
        ``read_provider`` / ``api_key`` (e.g. ``{"driver", "output_format"}`` for
        Nimble). Used to reject a key that belongs to a different backend so a
        silently-ignored option surfaces as a clear error.
    """

    run: Callable[[str, dict[str, str]], tuple[str | None, str | None]]
    keyless: bool
    options: frozenset[str] = frozenset()


class WebReadTool(Tool):
    """
    Unified web-read tool: fetch one URL's content as clean markdown.

    The spec must set ``read_provider`` to one of the engines in the
    ``_BACKENDS`` registry (``jina`` is keyless; ``nimble`` / ``firecrawl``
    need credentials) — there is no default and no env var fallback, so the
    spec is self-contained and the engine used is explicit.

    :param config: Spec-level config from config.yaml, e.g.
        ``{"read_provider": "nimble", "api_key": "..."}``.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        Create a unified web-read tool.

        :param config: Spec-level config with ``read_provider`` and
            (for keyed backends) ``api_key``.
        """
        self._config = config or {}

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"web_read"``.
        """
        return "web_read"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Fetch one web page's content as clean markdown, rendering "
            "JavaScript so client-side pages return real text. The keyed "
            "backends use managed retrieval for higher reliability on sites "
            "that refuse a plain request. Use for reading a specific URL you "
            "already have — articles, docs, product pages — especially when a "
            "plain fetch (web_fetch) returns an error or an empty page. "
            "This is a single-page read, not a crawler, and it calls a "
            "rate-limited or paid backend, so prefer web_fetch for simple "
            "public pages and reach for web_read when that fails. To find "
            "URLs, use web_search first."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        :returns: OpenAI-format function tool schema with a ``url`` param.
        """
        return {
            "type": "function",
            "function": {
                "name": "web_read",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": (
                                "A single http/https URL to fetch and read "
                                "(one page — not a site to crawl)."
                            ),
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run web_read synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of this tool.
        :returns: ``False`` — web_read always runs synchronously.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Fetch and return the content of the given URL.

        :param arguments: JSON-encoded dict with a ``url`` key.
        :param ctx: Tool execution context.
        :returns: Extracted content or an error message.
        """
        parsed, error = parse_json_object_arguments(arguments)
        if error is not None:
            return f"Error: {error}"
        assert parsed is not None
        url = parsed.get("url")
        if not isinstance(url, str) or not url.strip():
            return "Error: 'url' parameter is required."
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return "Error: 'url' must be an http:// or https:// URL."
        # Reject embedded control chars/whitespace: a newline would otherwise
        # break the "Source:" header line and let a caller forge a second one.
        if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
            return "Error: 'url' must not contain spaces or control characters."

        return _read(url, self._config)


def _read(url: str, config: dict[str, str]) -> str:
    """
    Read a URL using the backend named in config.

    :param url: The URL to read.
    :param config: Spec-level config. Required keys:

        - ``read_provider`` (required; no default): one of the engine names
          in ``_BACKENDS``
        - ``api_key``: API key for the chosen backend (keyless backends ignore it)

    :returns: Extracted content, or an error message (including when no
        ``read_provider`` is configured).
    """
    backend = config.get("read_provider")
    if not backend:
        return f"web_read error: no read_provider configured. {_backend_hint()}"

    engine = _BACKENDS.get(backend)
    if engine is None:
        return f"web_read error: unknown read_provider {backend!r}. {_backend_hint()}"

    key_error = _check_config_keys(backend, engine, config)
    if key_error is not None:
        return key_error

    content, diagnostic = engine.run(url, config)
    if diagnostic is not None:
        # Failure or non-content outcome (error / empty / blocked): the backend's
        # message is returned verbatim, with no Source header and no truncation.
        return diagnostic
    assert content is not None  # backends set exactly one of (content, diagnostic)
    return _finalize(content, url)


def _check_config_keys(backend: str, engine: _Backend, config: dict[str, str]) -> str | None:
    """
    Reject a config key the chosen backend ignores, so it can't fail silently.

    A key that is valid for a *different* backend (e.g. ``output_format`` is
    Nimble-only) is the common trap — someone sets it under ``jina`` and gets no
    error while it does nothing. The message names the backend that key belongs
    to when there is one.

    :param backend: The resolved ``read_provider`` name.
    :param engine: Its registry entry (its ``options`` are the extra keys it reads).
    :param config: The spec-level config dict.
    :returns: An error string, or ``None`` if every key is meaningful here.
    """
    allowed = _SHARED_CONFIG_KEYS | engine.options
    for key in config:
        if key in allowed:
            continue
        owners = sorted(name for name, b in _BACKENDS.items() if key in b.options)
        if owners:
            return (
                f"web_read error: {key!r} is not a {backend} option — it applies to "
                f"read_provider {' / '.join(owners)}. Remove it or switch read_provider."
            )
        return (
            f"web_read error: unknown config key {key!r} for read_provider {backend!r}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    return None


def _finalize(content: str, url: str) -> str:
    """
    Add a source header and cap length for a page body.

    Only ever called with real content — the dispatcher returns a backend's
    diagnostic (error / empty / blocked) verbatim before reaching here — so this
    always prepends the one-line ``Source:`` header (letting the model ground
    and cite the content) and caps the length at :data:`_MAX_CONTENT_CHARS`.

    :param content: The extracted page body.
    :param url: The read URL, used in the source header.
    :returns: The finalized tool output.
    """
    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS] + "\n\n[content truncated]"
    return f"Source: {url}\n\n{content}"


def _run_nimble(url: str, config: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Read via Nimble's Web API (``/v2/extract``); default driver ``vx8``.

    :param url: The URL to read.
    :param config: Must contain ``api_key``; ``driver``/``output_format`` optional.
    :returns: ``(content, diagnostic)`` — exactly one is non-None.
    """
    from omnigent.tools.builtins.web_read_nimble import _read_nimble

    return _read_nimble(url, config)


def _run_firecrawl(url: str, config: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Read via Firecrawl (``/v2/scrape``); LLM-native markdown, self-hostable.

    :param url: The URL to read.
    :param config: Must contain ``api_key``; ``proxy`` optional.
    :returns: ``(content, diagnostic)`` — exactly one is non-None.
    """
    from omnigent.tools.builtins.web_read_firecrawl import _read_firecrawl

    return _read_firecrawl(url, config)


def _run_jina(url: str, config: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Read via Jina Reader (``r.jina.ai``); keyless, LLM-ready markdown.

    :param url: The URL to read.
    :param config: ``api_key`` optional (lifts the rate limit).
    :returns: ``(content, diagnostic)`` — exactly one is non-None.
    """
    from omnigent.tools.builtins.web_read_jina import _read_jina

    return _read_jina(url, config)


# Single source of truth for the selectable backends. To add an engine, write
# its ``_run_*`` above and add one row here — the dispatch in ``_read`` and
# the error hint below both derive from this map, so nothing else needs editing.
# ``keyless`` drives only the hint wording (which engines need no ``api_key``).
_BACKENDS: dict[str, _Backend] = {
    "jina": _Backend(_run_jina, keyless=True),
    "nimble": _Backend(_run_nimble, keyless=False, options=frozenset({"driver", "output_format"})),
    "firecrawl": _Backend(_run_firecrawl, keyless=False, options=frozenset({"proxy"})),
}

# Config keys accepted by every backend (the rest are engine-specific ``options``).
_SHARED_CONFIG_KEYS = frozenset({"read_provider", "api_key"})


def _backend_hint() -> str:
    """Build the "set read_provider to one of ..." hint from ``_BACKENDS``.

    Derived from the registry so the error text can never drift from the set of
    engines that actually dispatch.

    :returns: A one-line hint naming the keyless and keyed engines.
    """
    keyless = [name for name, b in _BACKENDS.items() if b.keyless]
    keyed = [name for name, b in _BACKENDS.items() if not b.keyless]
    return (
        f"Set read_provider to one of: {', '.join(keyless)} (keyless, no API "
        f"key), or {', '.join(keyed)} with credentials for higher-reliability, "
        "higher-rate retrieval. No env var fallbacks — the spec is self-contained."
    )
