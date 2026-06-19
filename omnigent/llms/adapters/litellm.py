"""
LiteLLM provider adapter.

Routes Chat Completions requests through `litellm.acompletion`, which speaks the
OpenAI Chat Completions format natively and resolves 100+ providers from the
model string. This lets a user reach any LiteLLM-supported provider via a
``litellm/<model>`` model string without a dedicated Omnigent adapter.

Two modes, both via the same `litellm.acompletion` call:

- **SDK mode** (default): ``litellm/<litellm-model>`` — e.g.
  ``litellm/gpt-4o`` or ``litellm/anthropic/claude-3-5-sonnet`` — litellm
  resolves the provider from the model string and calls the API directly,
  authenticating with ``connection_params["api_key"]`` (or litellm's own env
  conventions when omitted).
- **Proxy mode**: set ``connection_params["base_url"]`` (or the adapter's
  default ``base_url``) to a running LiteLLM proxy — an OpenAI-compatible
  endpoint — and litellm routes the call there (passed as litellm's
  ``api_base``).

Because the model string is split on the FIRST ``"/"`` by the router, a nested
litellm model is preserved: ``litellm/anthropic/claude-3-5-sonnet`` reaches this
adapter as ``model="anthropic/claude-3-5-sonnet"``, exactly what litellm wants.

litellm is an optional dependency — install ``omnigent[litellm]``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import BaseAdapter

_logger = logging.getLogger(__name__)

# Default request timeouts (seconds), matching the OpenAI-compatible adapter.
_REQUEST_TIMEOUT = 120
_STREAM_TIMEOUT = 300


def _import_litellm() -> Any:
    """Import litellm lazily, with a helpful error when it is not installed.

    :returns: The imported ``litellm`` module.
    :raises OmnigentError: When the optional ``litellm`` package is missing.
    """
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise OmnigentError(
            "The 'litellm' provider requires the optional 'litellm' package. "
            "Install it with: pip install 'omnigent[litellm]'",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return litellm


def _to_chat_dict(obj: Any) -> dict[str, Any]:
    """Normalize a litellm response / stream chunk to a plain Chat Completions dict.

    litellm returns OpenAI-shaped ``ModelResponse`` / ``ModelResponseStream``
    objects (pydantic-style), already in Chat Completions format; this unwraps
    them to the plain dict the rest of Omnigent consumes.

    :param obj: A litellm response object, stream chunk, or dict.
    :returns: The Chat Completions dict.
    """
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            result = method()
            if isinstance(result, dict):
                return result
    return dict(obj)


class LiteLLMAdapter(BaseAdapter):
    """
    Adapter that delegates to ``litellm.acompletion``.

    API keys and the (optional) proxy base URL come from ``connection_params``
    at call time (the ``connection:`` block of an agent spec).

    :param base_url: Default LiteLLM-proxy base URL, or ``None`` for SDK mode.
        Overridable per call via ``connection_params["base_url"]``.
    """

    def __init__(self, base_url: str | None = None) -> None:
        """Store the optional default proxy base URL."""
        self._base_url = base_url.rstrip("/") if base_url else None

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
        connection_params: dict[str, str] | None,
        timeout: int | None,
    ) -> dict[str, Any]:
        """Build the ``litellm.acompletion`` keyword arguments.

        :param messages: Chat Completions messages.
        :param model: litellm model string (provider prefix already stripped of
            the leading ``litellm/``), e.g. ``"anthropic/claude-3-5-sonnet"``.
        :param tools: OpenAI-format tool schemas, or ``None``.
        :param stream: Whether to stream.
        :param extra: Additional Chat Completions kwargs (temperature,
            ``tool_choice``, ``reasoning_effort``, ...).
        :param connection_params: Per-call overrides — ``"api_key"`` and
            ``"base_url"`` (proxy mode).
        :param timeout: Request timeout in seconds, or ``None`` for the default.
        :returns: Keyword arguments for ``litellm.acompletion``.
        """
        params = connection_params or {}
        # Start from `extra` so the explicit, contract-defined args below always
        # win on any key collision (model/messages/stream/tools).
        kwargs: dict[str, Any] = dict(extra)
        kwargs["model"] = model
        kwargs["messages"] = messages
        kwargs["stream"] = stream
        if stream:
            # Ask for usage in the final stream chunk so token telemetry is
            # captured (mirrors the OpenAI-compatible adapter — the streaming
            # reducer only reads usage from a usage-bearing chunk). Overridable
            # via `extra` for providers/proxies that reject stream_options.
            kwargs.setdefault("stream_options", {"include_usage": True})
        if tools:
            kwargs["tools"] = tools
        if api_key := params.get("api_key"):
            kwargs["api_key"] = api_key
        # Proxy mode: route through a LiteLLM proxy / custom OpenAI-compatible
        # endpoint. litellm names this `api_base`.
        if base_url := (params.get("base_url") or self._base_url):
            kwargs["api_base"] = base_url
        kwargs["timeout"] = (
            timeout if timeout is not None else (_STREAM_TIMEOUT if stream else _REQUEST_TIMEOUT)
        )
        # Be forgiving across 100+ providers: drop a param a given provider does
        # not support (e.g. `reasoning_effort` on a non-reasoning model) rather
        # than erroring. Callers can override by setting `drop_params` in `extra`.
        kwargs.setdefault("drop_params", True)
        return kwargs

    async def chat_completions(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
        *,
        connection_params: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        """
        Send a Chat Completions request via ``litellm.acompletion``.

        :param messages: Chat Completions messages.
        :param model: litellm model string, e.g. ``"gpt-4o"`` or
            ``"anthropic/claude-3-5-sonnet"``.
        :param tools: OpenAI-format tool schemas, or ``None`` (passed through).
        :param stream: Enable streaming.
        :param extra: Additional kwargs forwarded to litellm.
        :param connection_params: Per-call overrides — ``"api_key"`` and
            ``"base_url"`` (proxy mode).
        :param timeout: Request timeout in seconds. ``None`` uses the default
            (120s non-streaming, 300s streaming).
        :returns: A Chat Completions response dict, or an async iterator of
            Chat Completions chunk dicts when ``stream=True``.
        :raises OmnigentError: When the optional ``litellm`` package is missing.
        """
        litellm = _import_litellm()
        kwargs = self._build_kwargs(
            messages, model, tools, stream, extra, connection_params, timeout
        )
        if stream:
            return self._stream(litellm, kwargs)
        response = await litellm.acompletion(**kwargs)
        return _to_chat_dict(response)

    async def _stream(self, litellm: Any, kwargs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield Chat Completions chunk dicts from a streaming litellm call.

        :param litellm: The imported litellm module.
        :param kwargs: Keyword arguments for ``litellm.acompletion`` (with
            ``stream=True``).
        :returns: Async iterator of Chat Completions chunk dicts.
        """
        response = await litellm.acompletion(**kwargs)
        async for chunk in response:
            yield _to_chat_dict(chunk)
