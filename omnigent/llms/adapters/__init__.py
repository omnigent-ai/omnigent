"""
Adapter registry — maps provider names to adapter instances.
"""

from __future__ import annotations

from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import BaseAdapter

# Lazy-initialized adapter cache. Each provider gets at most one
# adapter instance per process.
_adapter_cache: dict[str, BaseAdapter] = {}


def get_adapter(provider: str, **kwargs: Any) -> BaseAdapter:
    """
    Return an adapter instance for the given provider.

    Adapters are cached — the first call creates the instance and
    subsequent calls return the same one.

    :param provider: The provider identifier, e.g. ``"anthropic"``.
    :param kwargs: Extra keyword arguments forwarded to the adapter
        constructor (used by tests to override config).
    :returns: A :class:`BaseAdapter` subclass instance.
    :raises OmnigentError: If the provider is not supported.
    """
    if provider in _adapter_cache and not kwargs:
        return _adapter_cache[provider]

    adapter = _create_adapter(provider, **kwargs)
    if not kwargs:
        _adapter_cache[provider] = adapter
    return adapter


def _create_adapter(provider: str, **kwargs: Any) -> BaseAdapter:
    """
    Instantiate the correct adapter for the provider.

    Imports are lazy to avoid pulling in optional dependencies
    (boto3, google-auth) when they're not needed.

    :param provider: The provider identifier.
    :param kwargs: Extra kwargs for the adapter constructor.
    :returns: A :class:`BaseAdapter` instance.
    """
    # OpenAI-compatible providers — default base URLs only.
    # API keys come from connection_params at call time, not env vars.
    openai_compat_providers = {
        "openai": "https://api.openai.com/v1",
        "groq": "https://api.groq.com/openai/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "xai": "https://api.x.ai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434/v1",
        "moonshot": "https://api.moonshot.cn/v1",
    }

    if provider in openai_compat_providers:
        base_url = openai_compat_providers[provider]
        resolved_url = kwargs.get("base_url", base_url)
        if provider == "openai":
            # OpenAI supports the Responses API natively; use the
            # subclass that calls /v1/responses directly so reasoning
            # token events (reasoning_summary_text.delta etc.) flow through.
            from omnigent.llms.adapters.openai import OpenAIAdapter

            return OpenAIAdapter(base_url=resolved_url)
        from omnigent.llms.adapters.openai import OpenAICompatibleAdapter

        return OpenAICompatibleAdapter(base_url=resolved_url)

    if provider == "anthropic":
        from omnigent.llms.adapters.anthropic import AnthropicAdapter

        return AnthropicAdapter(**kwargs)

    if provider == "gemini":
        from omnigent.llms.adapters.gemini import GeminiAdapter

        return GeminiAdapter(**kwargs)

    if provider == "bedrock":
        from omnigent.llms.adapters.bedrock import BedrockAdapter

        return BedrockAdapter(**kwargs)

    if provider == "vertex":
        from omnigent.llms.adapters.vertex import VertexAdapter

        return VertexAdapter(**kwargs)

    if provider == "databricks":
        from omnigent.llms.adapters.databricks import DatabricksAdapter

        return DatabricksAdapter(**kwargs)

    if provider == "litellm":
        # Unified adapter over LiteLLM's 100+ providers. Imported lazily
        # so the optional ``litellm`` dependency is only needed when used.
        from omnigent.llms.adapters.litellm import LiteLLMAdapter

        return LiteLLMAdapter()

    all_providers = sorted(
        openai_compat_providers.keys()
        | {"anthropic", "gemini", "bedrock", "vertex", "databricks", "litellm"}
    )
    raise OmnigentError(
        f"Unknown provider {provider!r}. Supported: {all_providers}",
        code=ErrorCode.INVALID_INPUT,
    )


def clear_cache() -> None:
    """
    Clear the adapter cache. Useful for tests.
    """
    _adapter_cache.clear()
