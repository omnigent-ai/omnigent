"""
LiteLLM provider adapter.

Wraps LiteLLM's unified ``acompletion`` interface so any of the 100+
providers LiteLLM supports can be reached via the ``litellm/`` prefix
without a dedicated adapter — e.g. ``litellm/gpt-4o`` or
``litellm/anthropic/claude-3-5-sonnet``.

Two modes, selected by ``connection_params``:

- **SDK mode** (default): LiteLLM resolves the provider from the model
  string and calls the provider API directly. ``api_key`` may be passed
  via ``connection_params`` or left to LiteLLM's own env-var resolution.
- **Proxy mode**: when ``connection_params`` carries a ``base_url``, the
  request is routed through a LiteLLM proxy server — the model is
  prefixed with ``litellm_proxy/`` (LiteLLM's proxy provider) and
  ``base_url`` / ``api_key`` are forwarded to it.

LiteLLM already speaks the OpenAI Chat Completions format on both ends,
so — unlike the native adapters — no request/response translation is
needed: responses are returned as Chat Completions dicts via
``model_dump()`` (matching the dict shape the other adapters return).

The ``litellm`` package is an optional dependency (``omnigent[litellm]``);
it is imported lazily on first call so the base install never needs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import BaseAdapter

# Default timeouts mirror the OpenAI-compatible adapter.
_REQUEST_TIMEOUT = 120
_STREAM_TIMEOUT = 300

# LiteLLM's provider prefix for routing through a LiteLLM proxy server.
_PROXY_PREFIX = "litellm_proxy/"


class LiteLLMAdapter(BaseAdapter):
    """
    Adapter that delegates to LiteLLM's unified ``acompletion`` API.

    See the module docstring for SDK vs. proxy mode. Credentials and the
    optional proxy ``base_url`` come from ``connection_params`` at call
    time (from the ``connection:`` block in the agent spec's ``llm:``
    config), consistent with the OpenAI-compatible and Databricks
    adapters.
    """

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
        """
        Build the keyword arguments passed to ``litellm.acompletion``.

        Pure (no I/O) so it can be unit-tested without LiteLLM installed.

        :param messages: Chat Completions format messages.
        :param model: Model name without the ``litellm/`` prefix, e.g.
            ``"gpt-4o"`` or ``"anthropic/claude-3-5-sonnet"``.
        :param tools: OpenAI-format tool schemas, or ``None``.
        :param stream: Whether to enable streaming.
        :param extra: Additional provider kwargs (temperature, etc.).
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"``, ``"base_url"`` (the latter selects proxy mode).
        :param timeout: Request timeout in seconds, or ``None`` for the
            module default (120s non-streaming, 300s streaming).
        :returns: The kwargs dict for ``litellm.acompletion``.
        """
        params = connection_params or {}
        base_url = params.get("base_url")
        # Caller kwargs first, then our authoritative keys last so the
        # control-flow params (model/messages/stream/timeout) can't be
        # clobbered by ``extra``.
        kwargs: dict[str, Any] = {**extra}
        kwargs["model"] = _resolve_model(model, base_url)
        kwargs["messages"] = messages
        kwargs["stream"] = stream
        kwargs["timeout"] = (
            timeout if timeout is not None else (_STREAM_TIMEOUT if stream else _REQUEST_TIMEOUT)
        )
        if tools:
            kwargs["tools"] = tools
        if stream:
            # LiteLLM normalizes this across providers and emits a final
            # usage-bearing chunk, matching the OpenAI-compatible adapter.
            kwargs.setdefault("stream_options", {"include_usage": True})
        if api_key := params.get("api_key"):
            kwargs["api_key"] = api_key
        if base_url:
            # LiteLLM accepts ``api_base`` for the proxy / override URL.
            kwargs["api_base"] = base_url
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
        Send a Chat Completions request via LiteLLM.

        :param messages: Chat Completions format messages.
        :param model: Model name without the ``litellm/`` prefix, e.g.
            ``"gpt-4o"`` or ``"anthropic/claude-3-5-sonnet"``.
        :param tools: OpenAI-format tool schemas, or ``None``.
        :param stream: If ``True``, return an async iterator of Chat
            Completions chunk dicts. If ``False``, return a single
            response dict.
        :param extra: Additional provider kwargs (temperature, etc.).
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"`` and ``"base_url"`` (presence of ``base_url``
            selects proxy mode). ``None`` defers credential resolution to
            LiteLLM's own env-var handling.
        :param timeout: Request timeout in seconds. ``None`` uses the
            module default.
        :returns: A Chat Completions response dict when ``stream=False``,
            or an async iterator of chunk dicts when ``stream=True``.
        :raises OmnigentError: If the optional ``litellm`` dependency is
            not installed.
        """
        litellm = _import_litellm()
        kwargs = self._build_kwargs(
            messages, model, tools, stream, extra, connection_params, timeout
        )
        if stream:
            return self._stream(litellm, kwargs)
        response = await litellm.acompletion(**kwargs)
        return _to_dict(response)

    async def _stream(
        self,
        litellm: Any,
        kwargs: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Yield Chat Completions chunk dicts from a streaming call.

        :param litellm: The imported ``litellm`` module.
        :param kwargs: Pre-built ``acompletion`` kwargs with
            ``stream=True``.
        :yields: Chat Completions chunk dicts.
        """
        stream = await litellm.acompletion(**kwargs)
        async for chunk in stream:
            yield _to_dict(chunk)


def _resolve_model(model: str, base_url: str | None) -> str:
    """
    Resolve the model string passed to LiteLLM.

    In SDK mode the model is used as-is (LiteLLM infers the provider).
    In proxy mode (``base_url`` set) the model is prefixed with
    ``litellm_proxy/`` so LiteLLM routes through the proxy server rather
    than calling a provider directly — unless it already carries that
    prefix.

    :param model: Model name without the ``litellm/`` prefix.
    :param base_url: The proxy base URL when in proxy mode, else ``None``.
    :returns: The model string for ``litellm.acompletion``.
    """
    if base_url and not model.startswith(_PROXY_PREFIX):
        return f"{_PROXY_PREFIX}{model}"
    return model


def _import_litellm() -> Any:
    """
    Import and return the optional ``litellm`` module.

    :returns: The imported ``litellm`` module.
    :raises OmnigentError: If ``litellm`` is not installed.
    """
    try:
        import litellm
    except ImportError as exc:
        raise OmnigentError(
            "The 'litellm' provider requires the optional 'litellm'"
            " dependency. Install it with: pip install 'omnigent[litellm]'",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return litellm


def _to_dict(response: Any) -> dict[str, Any]:
    """
    Convert a LiteLLM response (or stream chunk) to a plain dict.

    LiteLLM returns pydantic ``ModelResponse`` / chunk objects in OpenAI
    Chat Completions shape. The other adapters return ``resp.json()``
    dicts, so normalize to a dict here. Already-dict inputs pass through.

    :param response: A LiteLLM ``ModelResponse``/chunk, or a dict.
    :returns: The response as a Chat Completions dict.
    :raises OmnigentError: If the object can't be converted to a dict.
    """
    if isinstance(response, dict):
        return response
    for attr in ("model_dump", "dict"):
        method = getattr(response, attr, None)
        if callable(method):
            result = method()
            if isinstance(result, dict):
                return result
    raise OmnigentError(
        f"Unexpected LiteLLM response type: {type(response).__name__}",
        code=ErrorCode.INTERNAL_ERROR,
    )
