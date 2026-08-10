"""Built-in tool: unified web scrape (bot-resistant single-URL fetch).

Fetches the full content of one URL as clean text/markdown through a managed
scraping backend that renders JavaScript and evades anti-bot protection — so an
agent can read pages that a plain HTTP fetch (``web_fetch``) gets a 403 or an
empty JS skeleton from. Complements ``web_search`` (which returns result links,
not page content).

The ``scrape_provider`` key in the spec selects the backend from the
``_BACKENDS`` registry; there is no default and no env var fallback, so the spec
is self-contained and the engine used is explicit.

Usage in config.yaml::

    tools:
      builtins:
        - name: web_scrape
          scrape_provider: nimble      # or firecrawl / jina
          api_key: ${NIMBLE_API_KEY}   # keyed backends need it; jina is keyless
          # driver: vx8                # nimble only (vx6/vx8/vx10)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

_logger = logging.getLogger(__name__)


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
            "Fetch the full content of a single web page as clean "
            "markdown, getting past bot protection and JavaScript "
            "rendering that block a plain fetch. Use for reading a "
            "specific URL (articles, docs, product pages) — especially "
            "when a normal fetch returns a 403 or an empty page. For "
            "finding URLs, use web_search first."
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
                            "description": "The URL to scrape (http or https).",
                        },
                    },
                    "required": ["url"],
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
    if engine is not None:
        return engine.run(url, config)

    if backend:
        return f"web_scrape error: unknown scrape_provider {backend!r}. {_backend_hint()}"
    return f"web_scrape error: no scrape_provider configured. {_backend_hint()}"


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
