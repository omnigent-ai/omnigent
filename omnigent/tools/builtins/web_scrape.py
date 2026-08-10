"""Built-in tool: unified web scrape (single-URL page reader).

Fetches the content of one URL as clean text/markdown through a managed
scraping backend that renders JavaScript and (on the keyed backends) handles
bot protection — so an agent can read pages that a plain HTTP fetch
(``web_fetch``) gets a 403 or an empty JS skeleton from. Complements
``web_search`` (which returns result links, not page content).

The ``scrape_provider`` key in the spec selects the backend from the
``_BACKENDS`` registry; there is no default and no ``api_key`` env-var fallback,
so the spec is self-contained and the engine used is explicit. (A backend may
read an ``OMNIGENT_*_BASE_URL`` override, used only to point tests at a mock.)

Usage in config.yaml::

    tools:
      builtins:
        - name: web_scrape
          scrape_provider: nimble      # or firecrawl / jina
          api_key: ${NIMBLE_API_KEY}   # keyed backends need it; jina is keyless
          # driver: vx8                # nimble only (vx6/vx8/vx10)
          # output_format: markdown    # nimble only (markdown/html)
          # proxy: auto                # firecrawl only (basic/enhanced/auto)

Responsible use:
    This tool performs a single, user-initiated fetch of one URL — it is not a
    crawler. It is intended for public, non-authenticated pages, and it does not
    bypass logins or paywalls: it reads a page as an unauthenticated visitor
    would. Fetching is delegated to the configured provider, which honors the
    site's ``robots.txt``. You remain responsible for using it in line with the
    target site's Terms of Service and applicable law (e.g. not scraping
    personal data without a lawful basis). This is general guidance, not legal
    advice.
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
    """A selectable ``scrape_provider`` engine in the ``_BACKENDS`` registry.

    :param run: Callable ``(url, config) -> content_or_error`` for the engine.
    :param keyless: True if the engine needs no ``api_key`` (drives hint text).
    """

    run: Callable[[str, dict[str, str]], str]
    keyless: bool


class WebScrapeTool(Tool):
    """
    Unified web-scrape tool: fetch one URL's content past bot protection.

    The spec must set ``scrape_provider`` to one of the engines in the
    ``_BACKENDS`` registry (``jina`` is keyless; ``nimble`` / ``firecrawl``
    need credentials) — there is no default and no env var fallback, so the
    spec is self-contained and the engine used is explicit.

    :param config: Spec-level config from config.yaml, e.g.
        ``{"scrape_provider": "nimble", "api_key": "..."}``.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        Create a unified web-scrape tool.

        :param config: Spec-level config with ``scrape_provider`` and
            (for keyed backends) ``api_key``.
        """
        self._config = config or {}

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"web_scrape"``.
        """
        return "web_scrape"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Fetch one web page's content as clean markdown, rendering "
            "JavaScript and (on the keyed backends) getting past bot "
            "protection that blocks a plain fetch. Use for reading a specific "
            "URL you already have — articles, docs, product pages — especially "
            "when a plain fetch returns a 403 or an empty page. This is a "
            "single-page read, not a crawler, and it calls a rate-limited or "
            "paid backend, so use it deliberately. To find URLs, use "
            "web_search first; for a quick unprotected fetch, web_fetch may "
            "suffice."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        :returns: OpenAI-format function tool schema with a ``url`` param.
        """
        return {
            "type": "function",
            "function": {
                "name": "web_scrape",
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
        Run web_scrape synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of this tool.
        :returns: ``False`` — web_scrape always runs synchronously.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a scrape of the given URL.

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

        return _scrape(url, self._config)


def _scrape(url: str, config: dict[str, str]) -> str:
    """
    Scrape a URL using the backend named in config.

    :param url: The URL to scrape.
    :param config: Spec-level config. Required keys:

        - ``scrape_provider`` (required; no default): one of the engine names
          in ``_BACKENDS``
        - ``api_key``: API key for the chosen backend (keyless backends ignore it)

    :returns: Extracted content, or an error message (including when no
        ``scrape_provider`` is configured).
    """
    backend = config.get("scrape_provider")

    engine = _BACKENDS.get(backend) if backend else None
    if engine is None:
        if backend:
            return f"web_scrape error: unknown scrape_provider {backend!r}. {_backend_hint()}"
        return f"web_scrape error: no scrape_provider configured. {_backend_hint()}"

    result = engine.run(url, config)
    return _finalize(result, url)


def _finalize(result: str, url: str) -> str:
    """
    Add a source header and cap length for a successful scrape.

    Backends signal failure with a message rather than raising; those are
    returned untouched (no header, no truncation) so the model sees the raw
    diagnostic. A real page body gets a one-line ``Source:`` header — so the
    model can ground and cite the content — and is capped at
    :data:`_MAX_CONTENT_CHARS`.

    :param result: The backend's return value (page content or an error/notice).
    :param url: The scraped URL, used in the source header.
    :returns: The finalized tool output.
    """
    if _is_backend_error(result):
        return result
    if len(result) > _MAX_CONTENT_CHARS:
        result = result[:_MAX_CONTENT_CHARS] + "\n\n[content truncated]"
    return f"Source: {url}\n\n{result}"


def _is_backend_error(result: str) -> bool:
    """
    :returns: True if ``result`` is a backend error/notice rather than content.

    Backends prefix diagnostics with ``Error:``, ``web_scrape ...``, or
    ``<Provider> scrape error:``; content never starts this way.
    """
    return result.startswith(("Error:", "web_scrape:", "web_scrape error:")) or (
        " scrape error:" in result[:40]
    )


def _run_nimble(url: str, config: dict[str, str]) -> str:
    """
    Scrape via Nimble's Web API (``/v2/extract``); default driver ``vx8``.

    :param url: The URL to scrape.
    :param config: Must contain ``api_key``; ``driver``/``output_format`` optional.
    :returns: Extracted content or an error message.
    """
    from omnigent.tools.builtins.web_scrape_nimble import _scrape_nimble

    return _scrape_nimble(url, config)


def _run_firecrawl(url: str, config: dict[str, str]) -> str:
    """
    Scrape via Firecrawl (``/v2/scrape``); LLM-native markdown, self-hostable.

    :param url: The URL to scrape.
    :param config: Must contain ``api_key``; ``proxy`` optional.
    :returns: Extracted markdown or an error message.
    """
    from omnigent.tools.builtins.web_scrape_firecrawl import _scrape_firecrawl

    return _scrape_firecrawl(url, config)


def _run_jina(url: str, config: dict[str, str]) -> str:
    """
    Scrape via Jina Reader (``r.jina.ai``); keyless, LLM-ready markdown.

    :param url: The URL to scrape.
    :param config: ``api_key`` optional (lifts the rate limit).
    :returns: Extracted markdown or an error message.
    """
    from omnigent.tools.builtins.web_scrape_jina import _scrape_jina

    return _scrape_jina(url, config)


# Single source of truth for the selectable backends. To add an engine, write
# its ``_run_*`` above and add one row here — the dispatch in ``_scrape`` and
# the error hint below both derive from this map, so nothing else needs editing.
# ``keyless`` drives only the hint wording (which engines need no ``api_key``).
_BACKENDS: dict[str, _Backend] = {
    "jina": _Backend(_run_jina, keyless=True),
    "nimble": _Backend(_run_nimble, keyless=False),
    "firecrawl": _Backend(_run_firecrawl, keyless=False),
}


def _backend_hint() -> str:
    """Build the "set scrape_provider to one of ..." hint from ``_BACKENDS``.

    Derived from the registry so the error text can never drift from the set of
    engines that actually dispatch.

    :returns: A one-line hint naming the keyless and keyed engines.
    """
    keyless = [name for name, b in _BACKENDS.items() if b.keyless]
    keyed = [name for name, b in _BACKENDS.items() if not b.keyless]
    return (
        f"Set scrape_provider to one of: {', '.join(keyless)} (keyless, no API "
        f"key), or {', '.join(keyed)} with credentials for stronger anti-bot / "
        "higher-rate scraping. No env var fallbacks — the spec is self-contained."
    )
