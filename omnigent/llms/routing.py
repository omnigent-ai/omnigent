"""
Provider routing — parse model strings and resolve adapters.

Model strings use ``"provider/model-name"`` format, e.g.
``"anthropic/claude-sonnet-4-20250514"``. If no provider prefix
is given, defaults to ``"openai"``.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.errors import ErrorCode, OmnigentError

# Known providers and their default base URLs.
# API keys come from connection_params at call time (llm.connection config),
# not from environment variables. Providers that require connection_params
# for their base URL (Bedrock, Vertex, Databricks) have None here.
PROVIDER_CONFIGS: dict[str, str | None] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "bedrock": None,
    "vertex": None,
    "databricks": None,
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "xai": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "moonshot": "https://api.moonshot.cn/v1",
}

_DEFAULT_PROVIDER = "openai"


@dataclass
class RoutedModel:
    """
    A parsed model string split into provider and model name.

    :param provider: The provider identifier, e.g. ``"anthropic"``.
    :param model: The model name without prefix, e.g.
        ``"claude-sonnet-4-20250514"``.
    """

    provider: str
    model: str


def parse_model_string(model: str) -> RoutedModel:
    """
    Parse a ``"provider/model-name"`` string into its components.

    If no ``"/"`` is present, the provider defaults to ``"openai"``
    for backward compatibility.

    :param model: The model string, e.g.
        ``"anthropic/claude-sonnet-4-20250514"`` or ``"gpt-5.4"``.
    :returns: A :class:`RoutedModel` with ``provider`` and ``model``.
    :raises OmnigentError: If the provider prefix is not recognized.
    """
    if "/" in model:
        provider, model_name = model.split("/", 1)
    else:
        provider = _DEFAULT_PROVIDER
        model_name = model

    if provider not in PROVIDER_CONFIGS:
        raise OmnigentError(
            f"Unknown provider {provider!r}. Known providers: {sorted(PROVIDER_CONFIGS)}",
            code=ErrorCode.INVALID_INPUT,
        )

    return RoutedModel(provider=provider, model=model_name)


# Maps model-string prefixes to the harness that should run them.
# Used by :func:`infer_harness_from_model` when a spec doesn't name
# a harness explicitly.
_HARNESS_FOR_MODEL_PREFIX: dict[str, str] = {
    "databricks-claude-": "claude-sdk",
    "anthropic/claude-": "claude-sdk",
    "databricks-gpt-": "openai-agents",
    "openai/gpt-": "openai-agents",
    "gpt-": "openai-agents",
    # xAI is OpenAI-compatible; provider prefix required (bare grok- defaults to openai).
    "xai/grok-": "openai-agents",
}


def web_search_native_passthrough_provider(
    model: str | None,
    harness: str | None,
) -> str | None:
    """
    Return the LLM provider for ``WebSearchTool`` when the harness supports
    the OpenAI-native ``web_search_preview`` passthrough, else ``None``.

    Only OpenAI Responses-compatible harnesses (``openai-agents`` / ``codex``)
    can execute the passthrough server-side; every other harness must get the
    function-tool schema or the tool is silently dropped (the schema has no
    ``name``). ``databricks-*`` models never use the passthrough (Databricks
    rejects it with HTTP 400). Shared by session-setup advertisement
    (``ToolManager._create_web_search``) and runner dispatch
    (``_execute_web_search_tool``) so the two sites cannot drift.

    :param model: The spec's ``executor.model``, or ``None``.
    :param harness: The spec's harness kind (``AgentSpec.harness_kind``);
        empty/``"omnigent"`` falls back to :func:`infer_harness_from_model`.
    :returns: The provider name for :class:`WebSearchTool`'s
        ``llm_provider`` (e.g. ``"openai"``), or ``None`` for
        function-tool mode.
    """
    if not model or model.startswith("databricks-"):
        return None
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.onboarding.provider_config import _EXECUTOR_TYPE_HARNESS_ALIASES

    if not harness or harness == "omnigent":
        harness = infer_harness_from_model(model) or harness
    canonical = canonicalize_harness(harness) or harness or ""
    canonical = _EXECUTOR_TYPE_HARNESS_ALIASES.get(canonical, canonical)
    if canonical in ("openai-agents", "codex"):
        return parse_model_string(model).provider
    return None


def infer_harness_from_model(model: str) -> str:
    """
    Return the harness name implied by a model string, or ``""``
    when no prefix in :data:`_HARNESS_FOR_MODEL_PREFIX` matches.

    An empty string has the same meaning as "no harness declared"
    in :class:`~omnigent.datamodel.ExecutorSpec` — callers that
    receive it propagate it unchanged and let the downstream
    validator surface a "harness required" error if one is needed.

    :param model: Model string from the spec's ``llm.model`` or
        ``executor.model`` field, e.g. ``"databricks-claude-sonnet-4"``.
    :returns: A harness name such as ``"claude-sdk"`` or
        ``"openai-agents"``, or ``""`` when *model* is unrecognised.
    """
    for prefix, harness in _HARNESS_FOR_MODEL_PREFIX.items():
        if model.startswith(prefix):
            return harness
    return ""
