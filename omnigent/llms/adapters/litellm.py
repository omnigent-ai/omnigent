"""
LiteLLM provider adapter.

Routes requests through the ``litellm`` SDK, which supports 100+
LLM providers (OpenAI, Anthropic, Bedrock, Vertex, Cohere, Mistral,
...) via a unified ``completion()`` / ``acompletion()`` interface.

Users specify ``litellm/<model>`` as the model string, where
``<model>`` is any litellm-compatible model identifier, e.g.
``litellm/gpt-4``, ``litellm/claude-3.5-sonnet``,
``litellm/bedrock/claude-3.5-sonnet``, etc.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from omnigent.llms.adapters.base import BaseAdapter

_logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 120
_STREAM_TIMEOUT = 300


class LiteLLMAdapter(BaseAdapter):
    """
    Adapter that delegates to ``litellm.acompletion()``.

    Supports two modes:

    - **SDK mode** (no ``base_url``): litellm resolves the provider
      from the model string and calls the provider API directly.
      Provider API keys are read from environment variables or
      passed via ``connection_params["api_key"]``.

    - **Proxy mode** (``base_url`` set): litellm sends requests to
      a running LiteLLM proxy server. ``api_key`` in
      ``connection_params`` is the proxy master/virtual key.
    """

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
        Send a chat completions request via litellm.

        :param messages: Chat Completions format messages.
        :param model: The litellm model string (without the
            ``litellm/`` routing prefix), e.g. ``"gpt-4"`` or
            ``"bedrock/claude-3.5-sonnet"``.
        :param tools: OpenAI-format tool schemas, or ``None``.
        :param stream: If ``True``, return an async iterator of
            chunk dicts.
        :param extra: Additional kwargs (temperature, etc.).
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"`` (provider key or proxy master key),
            ``"base_url"`` (LiteLLM proxy URL).
        :param timeout: Request timeout in seconds.
        :returns: Response dict or async iterator of chunk dicts.
        """
        try:
            import litellm
        except ImportError as exc:
            raise ImportError(
                "litellm is required for the litellm provider. "
                "Install it with: pip install 'omnigent[litellm]'"
            ) from exc

        params = connection_params or {}
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "drop_params": True,
            **extra,
        }
        if tools:
            kwargs["tools"] = tools

        api_key = params.get("api_key")
        if api_key:
            kwargs["api_key"] = api_key

        base_url = params.get("base_url")
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")

        if timeout is not None:
            kwargs["timeout"] = timeout
        elif stream:
            kwargs["timeout"] = _STREAM_TIMEOUT
        else:
            kwargs["timeout"] = _REQUEST_TIMEOUT

        if stream:
            return self._stream(litellm, kwargs)

        response = await litellm.acompletion(**kwargs)
        result: dict[str, Any] = response.model_dump()
        return result

    async def _stream(
        self,
        litellm: Any,
        kwargs: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream a litellm response and yield Chat Completions chunks.

        :param litellm: The imported litellm module.
        :param kwargs: The kwargs dict for ``litellm.acompletion``.
        :yields: Parsed Chat Completions chunk dicts.
        """
        response = await litellm.acompletion(**kwargs)
        async for chunk in response:
            yield chunk.model_dump()
