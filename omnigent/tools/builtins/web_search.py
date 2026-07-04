"""Built-in tool: unified web search.

Backend selection is fully determined by the agent spec:

- **OpenAI model** → passthrough to OpenAI's native
  ``web_search_preview`` (server-side, uses the LLM API key).
- **Other models** → use the ``search_provider`` named in the spec. Both
  keyless backends (no ``api_key``) and keyed ones (credentials for a
  sturdier / higher-rate backend) are supported; the ``_BACKENDS`` registry
  at the bottom of this module is the single source of truth for which
  engines exist. ``web_search`` never picks an engine for you — with no
  ``search_provider`` set it returns an error naming the options, so it is
  always explicit which engine ran. No env var fallbacks — the spec is
  self-contained.

Usage in config.yaml::

    # OpenAI model — web search is built-in, no config needed:
    tools:
      builtins:
        - web_search

    # Non-OpenAI model — name a search_provider (there is no default):
    tools:
      builtins:
        - name: web_search
          search_provider: duckduckgo   # a keyless backend, as an example
          # api_key: ${PERPLEXITY_API_KEY}   # required for keyed backends
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Backend:
    """A selectable ``search_provider`` engine in the ``_BACKENDS`` registry.

    :param run: Callable ``(query, config) -> result_or_error`` for the engine.
    :param keyless: True if the engine needs no ``api_key`` (drives hint text).
    """

    run: Callable[[str, dict[str, str]], str]
    keyless: bool


class WebSearchTool(Tool):
    """
    Unified web search tool with backend determined by the agent spec.

    When the agent uses an OpenAI model, this emits the native
    ``web_search_preview`` passthrough schema. For other models, the
    spec must set ``search_provider`` to one of the engines in the
    ``_BACKENDS`` registry (some keyless, some needing credentials) —
    there is no default and no env var fallback, so the spec is
    self-contained and the engine used is explicit.

    :param config: Spec-level config from config.yaml, e.g.
        ``{"search_provider": "perplexity", "api_key": "pplx-..."}``.
    :param llm_provider: The LLM provider name extracted from
        the model string, e.g. ``"openai"`` or ``"anthropic"``.
        When ``None``, falls back to function-tool mode.
    """

    def __init__(
        self,
        config: dict[str, str] | None = None,
        llm_provider: str | None = None,
    ) -> None:
        """
        Create a unified web search tool.

        :param config: Spec-level config with ``search_provider``
            and credentials. Required for non-OpenAI models.
        :param llm_provider: The agent's LLM provider, e.g.
            ``"openai"``. Determines whether to use passthrough
            or function-tool mode.
        """
        self._config = config or {}
        self._is_openai = llm_provider == "openai"

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"web_search"``.
        """
        return "web_search"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Quick web search — returns a comprehensive "
            "list of result links and snippets from a "
            "search engine. Good for broad discovery and "
            "finding URLs, but results may be slightly "
            "delayed vs. live web. For reading full page "
            "content or fetching the latest info from a "
            "specific URL, use web_fetch instead."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the tool schema, varying by provider.

        For OpenAI, returns the native ``web_search_preview``
        passthrough. For others, returns a function schema.

        :returns: OpenAI-format tool schema dict.
        """
        if self._is_openai:
            return {"type": "web_search_preview"}

        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Quick web search — returns a comprehensive "
                    "list of result links and snippets from a "
                    "search engine. Good for broad discovery and "
                    "finding URLs, but results may be slightly "
                    "delayed vs. live web. For reading full page "
                    "content or fetching the latest info from a "
                    "specific URL, use web_fetch instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run web_search synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of
            this tool, not the per-call arguments.
        :returns: ``False`` — web_search always runs synchronously.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a web search query.

        For OpenAI, this should never be called (passthrough).
        For others, delegates to the backend specified by
        ``search_provider`` in the spec config.

        :param arguments: JSON-encoded dict with a ``query`` key.
        :param ctx: Tool execution context.
        :returns: Search results or an error message.
        """
        if self._is_openai:
            raise RuntimeError(
                "web_search in OpenAI mode is a passthrough — "
                "the provider handles execution server-side. "
                "invoke() should never be called."
            )

        parsed: dict[str, Any] = json.loads(arguments)
        query = parsed.get("query")
        if not query:
            return "Error: 'query' parameter is required."

        return _search(query, self._config)


def _search(query: str, config: dict[str, str]) -> str:
    """
    Run a web search using the backend specified in config.

    The ``search_provider`` key in config determines which backend
    to use. No env var fallbacks — the spec must be self-contained.

    :param query: The search query string.
    :param config: Spec-level config. Required keys:

        - ``search_provider`` (required; no default): one of the engine
          names in the ``_BACKENDS`` registry
        - ``api_key``: API key for the chosen backend (keyless backends
          ignore it)
        - ``engine_id``: Required for Google only

    :returns: Search results, or an error message (including when no
        ``search_provider`` is configured).
    """
    backend = config.get("search_provider")

    engine = _BACKENDS.get(backend) if backend else None
    if engine is not None:
        return engine.run(query, config)

    # Fail loudly instead of silently picking an engine, so the user always
    # knows which engine ran and opts in explicitly (per maintainer review).
    if backend:
        return f"web_search error: unknown search_provider {backend!r}. {_backend_hint()}"
    return f"web_search error: no search_provider configured. {_backend_hint()}"


def _run_google(query: str, config: dict[str, str]) -> str:
    """
    Run a Google Custom Search query using spec config credentials.

    :param query: The search query.
    :param config: Must contain ``api_key`` and ``engine_id``.
    :returns: Formatted results or an error message.
    """
    from omnigent.tools.builtins.web_search_google import (
        _search_google,
    )

    api_key = config.get("api_key")
    engine_id = config.get("engine_id")
    if not api_key or not engine_id:
        return "Google web search requires api_key and engine_id in the web_search config."

    return _search_google(query, config)


def _run_perplexity(query: str, config: dict[str, str]) -> str:
    """
    Run a Perplexity search query using spec config credentials.

    :param query: The search query.
    :param config: Must contain ``api_key``.
    :returns: Answer with citations or an error message.
    """
    from omnigent.tools.builtins.web_search_perplexity import (
        _search_perplexity,
    )

    api_key = config.get("api_key")
    if not api_key:
        return "Perplexity web search requires api_key in the web_search config."

    return _search_perplexity(query, config)


def _run_nimble(query: str, config: dict[str, str]) -> str:
    """
    Run a Nimble web search query using spec config credentials.

    :param query: The search query.
    :param config: Must contain ``api_key``.
    :returns: Formatted results or an error message.
    """
    from omnigent.tools.builtins.web_search_nimble import (
        _search_nimble,
    )

    api_key = config.get("api_key")
    if not api_key:
        return "Nimble web search requires api_key in the web_search config."

    return _search_nimble(query, config)


def _run_tavily(query: str, config: dict[str, str]) -> str:
    """
    Run a Tavily web search query using spec config credentials.

    :param query: The search query.
    :param config: Must contain ``api_key``.
    :returns: Formatted results or an error message.
    """
    from omnigent.tools.builtins.web_search_tavily import (
        _search_tavily,
    )

    api_key = config.get("api_key")
    if not api_key:
        return "Tavily web search requires api_key in the web_search config."

    return _search_tavily(query, config)


def _run_duckduckgo(query: str, config: dict[str, str]) -> str:
    """
    Run a keyless DuckDuckGo HTML search.

    An opt-in, zero-credential backend — set ``search_provider: duckduckgo``
    to use it. Best-effort (the public HTML endpoint can rate-limit) — for
    robust/high-volume search, configure a keyed backend instead.

    :param query: The search query.
    :param config: Spec-level config (unused; DuckDuckGo needs no credentials).
    :returns: Formatted results or an error message.
    """
    from omnigent.tools.builtins.web_search_duckduckgo import (
        _search_duckduckgo,
    )

    return _search_duckduckgo(query, config)


def _run_keenable(query: str, config: dict[str, str]) -> str:
    """
    Run a Keenable web search query.

    Keyless by default: unlike the other backends, ``api_key`` is optional.
    Without it the keyless public endpoint is used; with it the
    authenticated endpoint is used and rate limits are lifted.

    :param query: The search query.
    :param config: May contain ``api_key`` and ``max_results`` (both optional).
    :returns: Formatted results or an error message.
    """
    from omnigent.tools.builtins.web_search_keenable import (
        _search_keenable,
    )

    return _search_keenable(query, config)


# Single source of truth for the selectable backends. To add an engine, write
# its ``_run_*`` above and add one row here — the dispatch in ``_search`` and
# the error hint below both derive from this map, so nothing else needs editing.
# ``keyless`` drives only the hint wording (which engines need no ``api_key``).
_BACKENDS: dict[str, _Backend] = {
    "duckduckgo": _Backend(_run_duckduckgo, keyless=True),
    "keenable": _Backend(_run_keenable, keyless=True),
    "google": _Backend(_run_google, keyless=False),
    "perplexity": _Backend(_run_perplexity, keyless=False),
    "nimble": _Backend(_run_nimble, keyless=False),
    "tavily": _Backend(_run_tavily, keyless=False),
}


def _backend_hint() -> str:
    """Build the "set search_provider to one of ..." hint from ``_BACKENDS``.

    Derived from the registry so the error text can never drift from the set of
    engines that actually dispatch.

    :returns: A one-line hint naming the keyless and keyed engines.
    """
    keyless = [name for name, b in _BACKENDS.items() if b.keyless]
    keyed = [name for name, b in _BACKENDS.items() if not b.keyless]
    return (
        f"Set search_provider to one of: {', '.join(keyless)} (keyless, no API "
        f"key), or {', '.join(keyed)} with credentials for a sturdier, "
        "higher-rate backend. No env var fallbacks — the spec is self-contained."
    )
