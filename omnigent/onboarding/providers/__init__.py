"""
Provider catalog and model discovery for onboarding.

Model lists are fetched live from the MLflow GitHub Release catalog
(``https://github.com/mlflow/mlflow/releases/download/model-catalog%2Flatest/{provider}.json``)
with a 1-hour in-process TTL cache. MLflow is **not** a required
dependency — the fetch uses only the stdlib ``urllib.request``.
Auth configuration (``PROVIDER_ENV_VARS``, ``get_provider_config``) is
omnigent-specific and lives here permanently.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

import cachetools


@dataclass
class ModelInfo:
    """
    Flat model metadata loaded from a catalog JSON file.

    :param name: The model identifier, e.g. ``"claude-sonnet-4-20250514"``.
    :param provider: The provider name, e.g. ``"anthropic"``.
    :param mode: The model mode, e.g. ``"chat"``, ``"embedding"``, or ``None``.
    :param supports_function_calling: Whether the model supports tool use, or
        ``None`` when the catalog does not report it.
    :param supports_reasoning: Whether the model supports reasoning, or
        ``None`` when unknown.
    :param supports_vision: Whether the model supports image input, or
        ``None`` when unknown.
    :param supports_structured_output: Whether the model supports response
        schemas, or ``None`` when unknown.
    :param max_input_tokens: Maximum input context window size, or ``None``.
    :param max_output_tokens: Maximum output tokens, or ``None``.
    :param input_price: Input price per million tokens, or ``None``.
    :param output_price: Output price per million tokens, or ``None``.
    :param cache_read_price: Cache-read price per million tokens, or ``None``.
    :param cache_write_price: Cache-write price per million tokens, or ``None``.
    """

    name: str
    provider: str
    mode: str | None = None
    supports_function_calling: bool | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None
    supports_structured_output: bool | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None
    cache_write_price: float | None = None


@dataclass
class AuthField:
    """
    A single credential field required by a provider's auth mode.

    :param name: Field identifier, e.g. ``"api_key"``.
    :param description: Human-readable label, e.g. ``"Anthropic API Key"``.
    :param secret: Whether the value should be masked in display.
    :param required: Whether the field is mandatory.
    """

    name: str
    description: str
    secret: bool
    required: bool


@dataclass
class AuthMode:
    """
    An authentication mode for a provider (e.g. API key, access keys, IAM role).

    :param mode_id: Short identifier, e.g. ``"api_key"``, ``"access_keys"``.
    :param display_name: Human-readable name, e.g. ``"API Key"``.
    :param description: Help text for the user.
    :param fields: Credential fields the user must supply.
    :param is_default: Whether this is the recommended default mode.
    """

    mode_id: str
    display_name: str
    description: str
    fields: list[AuthField]
    is_default: bool = False


@dataclass
class ProviderConfig:
    """
    Full auth configuration for a provider, with one or more auth modes.

    :param auth_modes: Available authentication modes.
    :param default_mode: The ``mode_id`` of the recommended default.
    """

    auth_modes: list[AuthMode]
    default_mode: str


# ---------------------------------------------------------------------------
# Catalog loading — live fetch from MLflow GitHub Release assets
# ---------------------------------------------------------------------------

_MLFLOW_CATALOG_URL = (
    "https://github.com/mlflow/mlflow/releases/download/model-catalog%2Flatest/{provider}.json"
)
_CATALOG_TTL_SECONDS = 3600
_catalog_cache: cachetools.TTLCache[str, dict[str, Any] | None] = cachetools.TTLCache(
    maxsize=64, ttl=_CATALOG_TTL_SECONDS
)
_catalog_cache_lock = threading.Lock()
_CATALOG_MISS = object()


def _download_provider_catalog(provider: str) -> dict[str, Any] | None:
    """
    Fetch ``{provider}.json`` from the MLflow GitHub Release catalog.

    Skipped when ``OMNIGENT_DISABLE_CATALOG_LOOKUP=1`` (set by the test
    suite to avoid network calls in CI).

    :param provider: Provider name, e.g. ``"anthropic"``.
    :returns: Parsed JSON dict (the full catalog file), or ``None`` on
        any network or parse error or when the lookup is disabled.
    """
    import os

    if os.environ.get("OMNIGENT_DISABLE_CATALOG_LOOKUP") == "1":
        return None
    url = _MLFLOW_CATALOG_URL.format(provider=provider)
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            result: dict[str, Any] = json.loads(resp.read())
        return result
    except Exception:
        return None


def _fetch_provider_catalog(provider: str) -> dict[str, Any]:
    """
    Return the MLflow catalog for *provider*, cached with a 1-hour TTL.

    Falls back to an empty dict on network failure (or when the lookup
    is disabled via ``OMNIGENT_DISABLE_CATALOG_LOOKUP``) so callers
    degrade gracefully rather than raising.

    :param provider: Provider name, e.g. ``"anthropic"``.
    :returns: Parsed catalog dict (``schema_version`` + ``models`` keys),
        or ``{}`` on failure.
    """
    with _catalog_cache_lock:
        cached = _catalog_cache.get(provider, _CATALOG_MISS)
        if cached is not _CATALOG_MISS:
            return cached or {}
    result = _download_provider_catalog(provider)
    with _catalog_cache_lock:
        _catalog_cache[provider] = result
    return result or {}


def _list_provider_names() -> list[str]:
    """
    Return the known provider names supported by the MLflow catalog.

    This is a static list matching the JSON files published in the
    MLflow GitHub Release assets, used to drive ``get_all_providers()``
    without requiring an upfront network scan. Provider variants (e.g.
    ``vertex_ai-llama_models``) are included; consolidation is applied
    later in ``get_all_providers()``.

    :returns: Sorted list of provider names.
    """
    return sorted(
        [
            "ai21",
            "aleph_alpha",
            "amazon_nova",
            "anthropic",
            "anyscale",
            "azure",
            "azure_ai",
            "azure_text",
            "bedrock",
            "bedrock_mantle",
            "cerebras",
            "cloudflare",
            "codestral",
            "cohere",
            "cohere_chat",
            "dashscope",
            "databricks",
            "deepinfra",
            "deepseek",
            "featherless_ai",
            "fireworks_ai",
            "friendliai",
            "gemini",
            "gigachat",
            "github_copilot",
            "gmi",
            "gradient_ai",
            "groq",
            "heroku",
            "hyperbolic",
            "lambda_ai",
            "lemonade",
            "llamagate",
            "meta_llama",
            "minimax",
            "mistral",
            "moonshot",
            "morph",
            "nebius",
            "nlp_cloud",
            "novita",
            "nscale",
            "oci",
            "ollama",
            "openai",
            "openrouter",
            "ovhcloud",
            "palm",
            "perplexity",
            "publicai",
            "replicate",
            "sagemaker",
            "sambanova",
            "sarvam",
            "snowflake",
            "text-completion-codestral",
            "text-completion-openai",
            "together_ai",
            "v0",
            "vercel_ai_gateway",
            "vertex_ai",
            "volcengine",
            "voyage",
            "wandb",
            "watsonx",
            "xai",
            "zai",
        ]
    )


# ---------------------------------------------------------------------------
# Provider consolidation (e.g. vertex_ai-* → vertex_ai)
# ---------------------------------------------------------------------------

_EXCLUDED_PROVIDERS = {"bedrock_converse"}

_PROVIDER_CONSOLIDATION: dict[str, Callable[[str], bool]] = {
    "vertex_ai": lambda p: p == "vertex_ai" or p.startswith("vertex_ai-"),
}


def _normalize_provider(provider: str) -> str:
    """
    Normalize provider name by consolidating variants into a single provider.

    For example, ``vertex_ai-llama_models`` becomes ``vertex_ai``.

    :param provider: Raw provider name from the catalog.
    :returns: Normalized provider name.
    """
    for normalized, matcher in _PROVIDER_CONSOLIDATION.items():
        if matcher(provider):
            return normalized
    return provider


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Popular providers shown first in selection UI, matching MLflow AI Gateway.
# Remaining providers follow in alphabetical order.
COMMON_PROVIDERS: list[str] = [
    "openai",
    "anthropic",
    "databricks",
    "bedrock",
    "gemini",
    "vertex_ai",
    "azure",
    "xai",
    "mistral",
    "groq",
    "deepseek",
    "openrouter",
    "ollama",
    "together_ai",
    "cohere",
    "fireworks_ai",
]


def get_all_providers() -> list[str]:
    """
    Return all available provider names from the bundled catalog.

    Popular providers (from :data:`COMMON_PROVIDERS`) are listed first,
    followed by the rest in alphabetical order. This matches the MLflow
    AI Gateway UI ordering so users see the most common choices at the
    top. Provider variants are consolidated (e.g. all ``vertex_ai-*``
    become ``vertex_ai``). Excluded providers (e.g. ``bedrock_converse``)
    are filtered out.

    :returns: Deduplicated list of provider names, popular first.
    """
    all_names: set[str] = set()
    for name in _list_provider_names():
        if name in _EXCLUDED_PROVIDERS:
            continue
        all_names.add(_normalize_provider(name))

    # Popular providers first (in COMMON_PROVIDERS order), then
    # remaining providers alphabetically.
    popular = [p for p in COMMON_PROVIDERS if p in all_names]
    rest = sorted(all_names - set(popular))
    return popular + rest


def get_models(provider: str) -> list[ModelInfo]:
    """
    Return all models for a provider, loaded from the catalog JSON files.

    For consolidated providers (e.g. ``vertex_ai``), models from all
    variant files are included.

    :param provider: Provider name, e.g. ``"anthropic"``.
    :returns: List of :class:`ModelInfo` for all models under that provider.
    """
    matching_files = [
        p
        for p in _list_provider_names()
        if _normalize_provider(p) == provider and p not in _EXCLUDED_PROVIDERS
    ]

    models: list[ModelInfo] = []
    seen: set[str] = set()

    for file_provider in matching_files:
        catalog = _fetch_provider_catalog(file_provider)
        for model_name, entry in catalog.get("models", {}).items():
            # Strip provider prefix if present (e.g. "gemini/gemini-2.5-flash")
            if model_name.startswith(f"{provider}/"):
                model_name = model_name.removeprefix(f"{provider}/")

            # Skip fine-tuned variants
            if model_name.startswith("ft:"):
                continue

            if model_name in seen:
                continue
            seen.add(model_name)

            context = entry.get("context_window", {})
            capabilities = entry.get("capabilities", {})
            pricing = entry.get("pricing", {})

            models.append(
                ModelInfo(
                    name=model_name,
                    provider=provider,
                    mode=entry.get("mode"),
                    supports_function_calling=capabilities.get("function_calling"),
                    supports_reasoning=capabilities.get("reasoning"),
                    supports_vision=capabilities.get("vision"),
                    supports_structured_output=capabilities.get("response_schema"),
                    max_input_tokens=context.get("max_input"),
                    max_output_tokens=context.get("max_output"),
                    input_price=pricing.get("input_per_million_tokens"),
                    output_price=pricing.get("output_per_million_tokens"),
                    cache_read_price=pricing.get("cache_read_per_million_tokens"),
                    cache_write_price=pricing.get("cache_write_per_million_tokens"),
                )
            )

    return models


_MODEL_PROVIDER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^claude-", re.IGNORECASE), "anthropic"),
    (re.compile(r"^(?:gpt-|o\d(?:-|$))", re.IGNORECASE), "openai"),
    (re.compile(r"^gemini-", re.IGNORECASE), "gemini"),
    (re.compile(r"^qwen(?:\d|-)", re.IGNORECASE), "dashscope"),
    (re.compile(r"^llama-", re.IGNORECASE), "meta_llama"),
    (re.compile(r"^mistral-", re.IGNORECASE), "mistral"),
)


def _inferred_catalog_provider(model: str) -> str | None:
    """Infer a catalog file from a stable vendor-family prefix."""
    for pattern, provider in _MODEL_PROVIDER_PATTERNS:
        if pattern.match(model):
            return provider
    return None


def _catalog_lookup_targets(model: str) -> list[tuple[str, str]]:
    """Return bounded provider/id pairs for a model catalog lookup."""
    normalized = model.split(":", 1)[0].strip()
    known_providers = set(get_all_providers())
    targets: list[tuple[str, str]] = []

    def _add(provider: str | None, model_id: str) -> None:
        if provider is not None and (provider, model_id) not in targets:
            targets.append((provider, model_id))

    if "/" in normalized:
        namespace, bare = normalized.split("/", 1)
        provider = namespace.lower()
        if provider in known_providers:
            _add(provider, bare)
            if provider == "databricks" and bare.lower().startswith("databricks-"):
                base = bare[len("databricks-") :]
                _add(_inferred_catalog_provider(base), base)
        else:
            _add("openrouter", normalized)
            _add(_inferred_catalog_provider(bare), bare)
        return targets

    if normalized.lower().startswith("databricks-"):
        _add("databricks", normalized)
        base = normalized[len("databricks-") :]
        _add(_inferred_catalog_provider(base), base)
    else:
        _add(_inferred_catalog_provider(normalized), normalized)
    _add("openrouter", normalized)
    return targets


def find_catalog_models(model: str) -> list[ModelInfo]:
    """Find exact or unambiguous-family candidates in the shared catalog.

    Provider-qualified ids query that provider directly. Vendor-namespaced ids
    query OpenRouter, while bare ids use stable family-to-provider routing with
    OpenRouter as a bounded fallback. Exact matches win; otherwise all entries
    sharing the id without its final hyphen segment are returned so callers can
    accept a field only when every candidate reports the same value.

    :param model: Provider-local, provider-qualified, or vendor-namespaced id.
    :returns: One exact match, compatible family-prefix matches, or an empty list.
    """
    family_matches: list[ModelInfo] = []
    for provider, lookup_id in _catalog_lookup_targets(model):
        models = get_models(provider)
        lookup_lower = lookup_id.lower()
        for candidate in models:
            names = {candidate.name.lower()}
            if provider == "openrouter":
                names.add(candidate.name.rsplit("/", 1)[-1].lower())
            if lookup_lower in names:
                return [candidate]
        if "-" not in lookup_id:
            continue
        prefixes = {lookup_lower.rsplit("-", 1)[0]}
        if provider == "openrouter":
            prefixes.add(lookup_lower.rsplit("/", 1)[-1].rsplit("-", 1)[0])
        family_matches.extend(
            candidate
            for candidate in models
            if any(
                candidate_name.startswith(prefix)
                for prefix in prefixes
                for candidate_name in (
                    candidate.name.lower(),
                    candidate.name.rsplit("/", 1)[-1].lower(),
                )
            )
        )
    return family_matches


def get_chat_models(provider: str) -> list[ModelInfo]:
    """
    Return only chat-capable models for a provider, newest first.

    Filters to ``mode="chat"`` and sorts by version number
    (descending), then release date (newest first), matching the
    MLflow AI Gateway UI ordering.

    :param provider: Provider name, e.g. ``"anthropic"``.
    :returns: Sorted list of chat-mode :class:`ModelInfo` instances.
    """
    chat = [m for m in get_models(provider) if m.mode == "chat"]
    return _sort_models_newest_first(chat)


# Name tokens that mark a "chat"-mode model as a *specialty* modality
# (audio I/O, low-latency realtime, web-search-augmented, speech
# transcription / synthesis, image generation). The catalog tags all of
# these with ``mode="chat"`` and ``function_calling=True``, so they sort to
# the top of :func:`get_chat_models` by date even though they are poor
# general-purpose coding-agent defaults (e.g. ``gpt-audio-mini`` and
# ``gpt-realtime`` outrank ``gpt-5.4`` for OpenAI by release date). They are
# excluded only when *picking a fallback default* — they remain in the full
# :func:`get_chat_models` list so the interactive picker can still offer them.
_SPECIALTY_MODEL_TOKENS: tuple[str, ...] = (
    "audio",
    "realtime",
    "search",
    "transcribe",
    "tts",
    "image",
)

# Provider → name token of the preferred *default* tier. A default model
# must be broadly accessible (it's what a fresh user gets before they pick
# one), so we steer toward the balanced tier rather than the premium one:
# Anthropic's ``opus`` is gated on some plans / can 4xx with "no access",
# whereas ``sonnet`` is available on every plan and is the conventional
# coding-agent default (Cursor/Cline/etc.). The premium tier is still
# selectable via ``configure harness`` / ``/model``. Providers absent from
# this map keep the plain "newest general-purpose model" rule (e.g. OpenAI's
# flagship ``gpt-*`` is broadly accessible, so no steering is needed).
_PREFERRED_DEFAULT_TIER_TOKEN: dict[str, str] = {
    "anthropic": "sonnet",
}

# Required provider → case-insensitive model-id substring. If no entry matches,
# onboarding asks explicitly instead of choosing an unexpected alternative.
_REQUIRED_DEFAULT_TIER_TOKEN: dict[str, str] = {
    "openrouter": "kimi",
}


def default_chat_model(
    provider: str,
    *,
    allowed_models: Collection[str] | None = None,
) -> str | None:
    """
    Return the catalog's canonical default chat model for a provider.

    This is the bundled catalog's notion of "the model to use when neither
    the agent spec nor the provider config names one". The rule, chosen so
    the default is both sensible and broadly accessible:

    1. Start from :func:`get_chat_models` (chat-mode models, newest first).
    2. Drop specialty modalities (audio / realtime / search / transcribe /
       tts / image — see :data:`_SPECIALTY_MODEL_TOKENS`), which the catalog
       also tags ``mode="chat"`` and which would otherwise outrank the
       flagship text model by release date for some providers (OpenAI's
       ``gpt-audio-*`` / ``gpt-realtime-*`` sort above ``gpt-5.4``).
    3. If the provider requires a default *tier*
       (:data:`_REQUIRED_DEFAULT_TIER_TOKEN`), return its newest model or
       ``None`` when the catalog offers none.
    4. If the provider has a preferred default *tier*
       (:data:`_PREFERRED_DEFAULT_TIER_TOKEN`), return the newest remaining
       model of that tier — so a fresh user gets a model their key can
       actually use. Fall back to the newest remaining general-purpose model
       when no model matches the tier.

    ``allowed_models`` constrains the catalog rule so callers can apply family
    or routing compatibility before selection.

    :param provider: Provider name, e.g. ``"anthropic"`` or ``"openai"``.
    :param allowed_models: Optional model ids eligible for selection.
    :returns: The selected catalog model id, or ``None`` when the catalog has
        no chat model for that provider.
    """
    allowed = set(allowed_models) if allowed_models is not None else None
    general: list[str] = []
    for model in get_chat_models(provider):
        if allowed is not None and model.name not in allowed:
            continue
        lowered = model.name.lower()
        if any(token in lowered for token in _SPECIALTY_MODEL_TOKENS):
            continue
        general.append(model.name)

    required_token = _REQUIRED_DEFAULT_TIER_TOKEN.get(provider)
    if required_token is not None:
        return next((name for name in general if required_token in name.lower()), None)

    preferred_token = _PREFERRED_DEFAULT_TIER_TOKEN.get(provider)
    if preferred_token is not None:
        for name in general:
            if preferred_token in name.lower():
                return name
    # No tier preference, or no model matched it: newest general-purpose model.
    return general[0] if general else None


# ---------------------------------------------------------------------------
# Model sorting — newest/best models first
# ---------------------------------------------------------------------------

# Vendor marker used to isolate version tokens from provider prefixes, model
# sizes, and dates. The previous optional marker treated any number as a model
# version, so a size such as ``120b`` could outrank a newer vendor model.
_VERSION_FAMILY_PATTERN = re.compile(
    r"(?:^|[-/])(?:gpt|claude|llama|gemini|deepseek(?:-v)?|o)(?=[-/]?\d|[-/])"
)

# Matches dates: 2025-04-14, 20250414, 20241022
_DATE_PATTERN = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


def _extract_model_version(name: str) -> float:
    """
    Extract the primary version number from a model name.

    :param name: Model name, e.g. ``"gpt-4.1-2025-04-14"``.
    :returns: Version as float, or ``0.0`` if none found.
    """
    family = _VERSION_FAMILY_PATTERN.search(name.lower())
    if family is None:
        return 0.0
    suffix = _DATE_PATTERN.sub("", name[family.end() :])
    tokens = [
        token for token in re.split(r"[-/]", suffix) if re.fullmatch(r"\d+(?:\.\d+)?", token)
    ]
    if not tokens:
        return 0.0
    primary = float(tokens[0])
    if "." not in tokens[0] and len(tokens) > 1 and len(tokens[1]) == 1:
        return float(f"{tokens[0]}.{tokens[1]}")
    return primary


def _extract_model_date(name: str) -> int:
    """
    Extract a date as an integer from a model name for sorting.

    :param name: Model name, e.g. ``"gpt-4-2024-08-06"``.
    :returns: Date as YYYYMMDD integer, or ``0`` if none found.
    """
    match = _DATE_PATTERN.search(name)
    if match:
        return int(match.group(1) + match.group(2) + match.group(3))
    return 0


def _sort_models_newest_first(models: list[ModelInfo]) -> list[ModelInfo]:
    """
    Sort models by version (descending), date (newest first), then name.

    Matches MLflow AI Gateway's ``sortModelsByDate()`` logic so that
    newer, more capable models appear at the top of the selection list.

    :param models: Unsorted model list.
    :returns: Sorted model list, newest/highest version first.
    """
    return sorted(
        models,
        key=lambda m: (
            -_extract_model_version(m.name),
            -_extract_model_date(m.name),
            m.name,
        ),
    )


# ---------------------------------------------------------------------------
# Auth mode definitions
# ---------------------------------------------------------------------------

# Providers with multiple auth modes. For simple API-key providers,
# a default mode is generated dynamically by get_provider_config().
_PROVIDER_AUTH_MODES: dict[str, dict[str, dict[str, Any]]] = {
    "bedrock": {
        "api_key": {
            "display_name": "API Key",
            "description": "Use Amazon Bedrock API Key (bearer token)",
            "default": True,
            "fields": [
                {
                    "name": "api_key",
                    "description": "Amazon Bedrock API Key",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "aws_region_name",
                    "description": "AWS Region",
                    "secret": False,
                    "required": True,
                },
            ],
        },
        "access_keys": {
            "display_name": "Access Keys",
            "description": "Use AWS Access Key ID and Secret Access Key",
            "fields": [
                {
                    "name": "aws_access_key_id",
                    "description": "AWS Access Key ID",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "aws_secret_access_key",
                    "description": "AWS Secret Access Key",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "aws_region_name",
                    "description": "AWS Region (e.g., us-east-1)",
                    "secret": False,
                    "required": False,
                },
            ],
        },
    },
    "azure": {
        "api_key": {
            "display_name": "API Key",
            "description": "Use Azure OpenAI API Key",
            "default": True,
            "fields": [
                {
                    "name": "api_key",
                    "description": "Azure OpenAI API Key",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "api_base",
                    "description": "Azure OpenAI endpoint URL",
                    "secret": False,
                    "required": True,
                },
                {
                    "name": "api_version",
                    "description": "API version (e.g., 2024-02-01)",
                    "secret": False,
                    "required": True,
                },
            ],
        },
    },
    "vertex_ai": {
        "service_account_json": {
            "display_name": "Service Account JSON",
            "description": "Use GCP Service Account credentials (JSON key file contents)",
            "default": True,
            "fields": [
                {
                    "name": "vertex_credentials",
                    "description": "Service Account JSON key file contents",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "vertex_project",
                    "description": "GCP Project ID",
                    "secret": False,
                    "required": True,
                },
                {
                    "name": "vertex_location",
                    "description": "GCP Region (e.g., us-central1)",
                    "secret": False,
                    "required": False,
                },
            ],
        },
    },
    "databricks": {
        "pat_token": {
            "display_name": "Personal Access Token",
            "description": "Use Databricks Personal Access Token",
            "default": True,
            "fields": [
                {
                    "name": "api_key",
                    "description": "Databricks Personal Access Token",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "api_base",
                    "description": "Databricks workspace URL",
                    "secret": False,
                    "required": True,
                },
            ],
        },
    },
    "sagemaker": {
        "access_keys": {
            "display_name": "Access Keys",
            "description": "Use AWS Access Key ID and Secret Access Key",
            "default": True,
            "fields": [
                {
                    "name": "aws_access_key_id",
                    "description": "AWS Access Key ID",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "aws_secret_access_key",
                    "description": "AWS Secret Access Key",
                    "secret": True,
                    "required": True,
                },
                {
                    "name": "aws_region_name",
                    "description": "AWS Region (e.g., us-east-1)",
                    "secret": False,
                    "required": True,
                },
            ],
        },
    },
}

# Display names for providers that don't title-case cleanly.
# Copied from MLflow AI Gateway's PROVIDER_DISPLAY_NAMES.
# Providers not in this dict fall back to .replace("_", " ").title().
_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "bedrock": "Amazon Bedrock",
    "gemini": "Google Gemini",
    "vertex_ai": "Google Vertex AI",
    "azure": "Azure OpenAI",
    "groq": "Groq",
    "databricks": "Databricks",
    "xai": "xAI",
    "cohere": "Cohere",
    "mistral": "Mistral AI",
    "together_ai": "Together AI",
    "fireworks_ai": "Fireworks AI",
    "replicate": "Replicate",
    "huggingface": "Hugging Face",
    "ai21": "AI21",
    "perplexity": "Perplexity",
    "deepinfra": "DeepInfra",
    "cerebras": "Cerebras",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
}


def format_provider_name(provider: str) -> str:
    """
    Return a human-readable display name for a provider.

    Uses a lookup table for providers that don't title-case cleanly
    (e.g. ``"openai"`` → ``"OpenAI"``). Falls back to
    ``provider.replace("_", " ").title()`` for unknown providers.

    :param provider: Provider identifier, e.g. ``"openai"``.
    :returns: Display name, e.g. ``"OpenAI"``.
    """
    if provider in _PROVIDER_DISPLAY_NAMES:
        return _PROVIDER_DISPLAY_NAMES[provider]
    return provider.replace("_", " ").title()


# Env var names for simple API-key providers (used for non-interactive mode).
PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "togetherai": "TOGETHERAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "ai21": "AI21_API_KEY",
    "fireworks_ai": "FIREWORKS_AI_API_KEY",
    "perplexity": "PERPLEXITYAI_API_KEY",
    "together_ai": "TOGETHERAI_API_KEY",
    "replicate": "REPLICATE_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "cloudflare": "CLOUDFLARE_API_KEY",
    "huggingface": "HUGGINGFACE_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY",
    "novita": "NOVITA_API_KEY",
}


def get_provider_config(provider: str) -> ProviderConfig:
    """
    Return the auth configuration for a provider.

    For providers with multiple auth modes (bedrock, azure, vertex_ai,
    databricks, sagemaker), returns the full structure. For simple
    API-key providers, returns a single default auth mode.

    :param provider: Provider name, e.g. ``"openai"`` or ``"bedrock"``.
    :returns: :class:`ProviderConfig` with available auth modes.
    """
    if provider in _PROVIDER_AUTH_MODES:
        modes: list[AuthMode] = []
        default_mode_id: str | None = None
        for mode_id, mode_def in _PROVIDER_AUTH_MODES[provider].items():
            fields = [
                AuthField(
                    name=f["name"],
                    description=f["description"],
                    secret=f["secret"],
                    required=f["required"],
                )
                for f in mode_def["fields"]
            ]
            is_default = mode_def.get("default", False)
            if is_default:
                default_mode_id = mode_id
            modes.append(
                AuthMode(
                    mode_id=mode_id,
                    display_name=mode_def["display_name"],
                    description=mode_def["description"],
                    fields=fields,
                    is_default=is_default,
                )
            )
        return ProviderConfig(
            auth_modes=modes,
            default_mode=default_mode_id or modes[0].mode_id,
        )

    # Simple API-key provider — generate a default mode.
    display = format_provider_name(provider)
    return ProviderConfig(
        auth_modes=[
            AuthMode(
                mode_id="api_key",
                display_name="API Key",
                description=f"Use {display} API Key",
                fields=[
                    AuthField(
                        name="api_key",
                        description=f"{display} API Key",
                        secret=True,
                        required=True,
                    ),
                ],
                is_default=True,
            ),
        ],
        default_mode="api_key",
    )
