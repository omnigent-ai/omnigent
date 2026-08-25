"""Deterministic model enumeration for sub-agent model awareness.

Backs the ``sys_list_models`` runner builtin: for each sub-agent worker
of an orchestrator's spec (plus the orchestrator brain itself), resolve
the model provider the spawn/launch paths would actually use — the same
precedence as :func:`omnigent.runtime.workflow._resolve_provider_for_build`
followed by the legacy auth fallthrough the spawn-env builders apply —
and enumerate that provider's live model listing. The resolved provider
*kind* is also what the ``sys_session_send`` dispatch gate consults for
canonical→gateway-local model-id normalization
(:func:`omnigent.model_override.normalize_model_for_provider`).

Enumeration is deterministic per provider kind:

- ``databricks`` → ``GET <workspace>/api/2.0/serving-endpoints`` with a
  token minted from the profile (source ``"gateway"``).
- ``key`` with the ``anthropic`` family → ``GET <base_url>/v1/models``
  with ``x-api-key`` headers (source ``"anthropic-api"``).
- ``key`` (openai family) / ``gateway`` / ``local`` →
  ``GET <base_url>/v1/models`` with a bearer token (source
  ``"openai-compatible"``).
- ``subscription`` → live CLI discovery for Cursor; curated static aliases for
  CLIs without a listing API (source ``"static"``, ``verified: false``).
- ``cli-config`` → the codex curated static list (source ``"static"``,
  ``verified: false`` — the credential lives in the CLI's own config
  file and is resolved by the CLI at launch).
- anything unresolvable → source ``"none"`` with an explanatory note,
  which doubles as a dead-worker preflight signal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

import click
import httpx
from cachetools import TTLCache

from omnigent._platform import default_shell_argv
from omnigent.json_types import JsonObject as _JsonObject
from omnigent.llms.anthropic_model_metadata import parse_anthropic_model_metadata
from omnigent.model_metadata import (
    ModelCapability,
    ModelCostTier,
    ModelIntent,
    ModelMetadata,
    ModelWireAPI,
)
from omnigent.model_override import is_codex_compatible_model, model_family_mismatch
from omnigent.model_resolver import (
    ModelResolution,
    ModelResolutionError,
    ModelResolutionRequest,
    resolve_model,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    KEY_KIND,
    OPENAI_FAMILY,
    SUBSCRIPTION_KIND,
    ProviderEntry,
)
from omnigent.pi_model_compatibility import unsupported_in_pi
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

if TYPE_CHECKING:
    from omnigent.onboarding.providers import ModelInfo
    from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)

# Sentinel kind for "no usable provider resolved" rows.
NONE_KIND = "none"

# ~5 min: provider listings change rarely; a turn that fans out many
# dispatches should never re-fetch per call.
_CATALOG_TTL_S = 300.0
_HTTP_TIMEOUT_S = 10.0
_AUTH_COMMAND_TIMEOUT_S = 15.0

# Header version the Anthropic models API requires (same value as
# ``omnigent/llms/adapters/anthropic.py``).
_ANTHROPIC_API_VERSION = "2023-06-01"

# Name tokens that mark a Databricks serving endpoint as an LLM when the
# endpoint carries no usable ``task`` field.
_LLM_NAME_TOKENS = ("claude", "gpt", "codex", "gemini", "llama", "qwen", "kimi")

# Chat-capable endpoint tasks ("llm/v1/chat"); embeddings/rerankers don't match.
_LLM_TASK_TOKENS = ("chat", "completion")

# DATABRICKS-PATCH(model-services-scoped-listing): scope + page the Unity
# Catalog model-services listing. Mirrors
# ``databricks_model_discovery._MODEL_SERVICES_PARENT`` /
# ``_MODEL_SERVICES_MAX_RESULTS`` — including the parameter *name*, so both
# callers of this endpoint ask for a page size the API actually honors.
_MODEL_SERVICES_PARENT = "schemas/system.ai"
_MODEL_SERVICES_MAX_RESULTS = 100
_MODEL_SERVICES_MAX_PAGES = 100

_ProviderHarness: TypeAlias = Literal[
    "claude-sdk",
    "codex",
    "pi",
    "openai-agents-sdk",
    "antigravity",
    "kimi",
    "qwen",
]

# Harness spellings -> the workflow harness whose provider resolution they
# share; natives resolve via their SDK sibling (the resolve_native_* rule).
_PROVIDER_RESOLUTION_HARNESS: dict[str, _ProviderHarness] = {
    "claude-sdk": "claude-sdk",
    "claude_sdk": "claude-sdk",
    "claude": "claude-sdk",
    "claude-native": "claude-sdk",
    "native-claude": "claude-sdk",
    "codex": "codex",
    "codex-native": "codex",
    "native-codex": "codex",
    "pi": "pi",
    "pi-native": "pi",
    "native-pi": "pi",
    "openai-agents": "openai-agents-sdk",
    "openai-agents-sdk": "openai-agents-sdk",
    "agents_sdk": "openai-agents-sdk",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "google-antigravity": "antigravity",
    # Kimi Code CLI is multi-provider; it shares no resolution path with an
    # existing harness. The identity entry keeps callers that iterate this
    # map (e.g. ``list_models_for_worker``) finding the harness so they
    # don't fall through to a noisy "unknown harness" branch.
    "kimi": "kimi",
    "kimi-code": "kimi",
    # Native Kimi TUI harness shares the multi-provider kimi resolution path.
    "kimi-native": "kimi",
    "qwen": "qwen",
    # The native agy TUI bridge resolves its provider via the SDK sibling,
    # mirroring the claude-native -> claude-sdk rule above.
    "antigravity-native": "antigravity",
    "native-antigravity": "antigravity",
}

# cursor-agent always routes through its own stored login — there is no
# omnigent-side provider config for it to resolve (or fail), so resolution
# short-circuits to a subscription-style readout instead of reporting the
# harness as having "no model-provider resolution".
_CURSOR_HARNESSES: frozenset[str] = frozenset({"cursor", "cursor-native", "native-cursor"})

# Preferred inline family per single-family harness (pi consumes both).
_KEY_AUTH_FAMILY: dict[str, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
    "codex": OPENAI_FAMILY,
    "openai-agents-sdk": OPENAI_FAMILY,
    "antigravity": OPENAI_FAMILY,
    "qwen": OPENAI_FAMILY,
}

# Multi-family providers (pi): anthropic first, matching _apply_provider_to_pi.
_FAMILY_PREFERENCE = (ANTHROPIC_FAMILY, OPENAI_FAMILY)


@dataclass(frozen=True)
class ModelEntry:
    """One model a worker can run.

    :param id: Provider-local model id, e.g.
        ``"databricks-claude-sonnet-4-6"`` or ``"gpt-5.4-mini"``.
    :param family: Vendor family token — ``"claude"``, ``"openai"``, or
        ``"other"``.
    :param metadata: Provider-neutral capabilities and limits. Metadata fields
        remain unknown when the listing API reports only a model id.
    """

    id: str
    family: str
    metadata: ModelMetadata = field(default_factory=ModelMetadata)

    @property
    def context_window(self) -> int | None:
        """Return the normalized context window for compatibility callers."""
        return self.metadata.context_window


@dataclass(frozen=True)
class ModelListing:
    """A worker's enumerated model list plus its provenance.

    :param source: Where the list came from — ``"gateway"``,
        ``"openai-compatible"``, ``"anthropic-api"``, ``"cli"``,
        ``"static"``, or ``"none"``.
    :param verified: ``True`` when the list was fetched live from the
        provider; ``False`` for static/curated or empty listings.
    :param models: The enumerated models, e.g.
        ``(ModelEntry(id="databricks-gpt-5-4", family="openai"),)``.
    :param note: Human-readable provenance / failure explanation.
    """

    source: str
    verified: bool
    models: tuple[ModelEntry, ...]
    note: str


@dataclass(frozen=True)
class ResolvedModelProvider:
    """The model provider a worker's spawn/launch path would route through.

    :param kind: Provider kind — ``"key"`` / ``"gateway"`` / ``"local"``
        / ``"subscription"`` / ``"databricks"`` / ``"cli-config"`` from
        the provider config layer, or ``"none"`` when no usable provider
        resolved.
    :param family: ``"anthropic"`` / ``"openai"`` for inline-family
        kinds, else ``None``.
    :param profile: Databricks profile for ``kind="databricks"``, e.g.
        ``"my-profile"``; ``None`` falls back to the ``[DEFAULT]`` section.
    :param base_url: Endpoint base URL for inline-family kinds, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param api_key: Resolved static credential for inline-family kinds.
        Never serialized into tool output.
    :param auth_command: Shell command printing a bearer token, for
        providers configured with a dynamic credential.
    :param cli: ``"claude"`` / ``"codex"`` / ``"cursor-agent"`` for
        ``kind="subscription"``; ``"codex"`` for ``kind="cli-config"``.
    :param detail: Non-secret descriptor of how the provider resolved,
        e.g. ``"provider 'openrouter'"`` — used in listing notes.
    """

    kind: str
    family: str | None = None
    profile: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    auth_command: str | None = None
    cli: str | None = None
    detail: str = ""


def is_direct_openai_provider(provider: ResolvedModelProvider) -> bool:
    """Return whether *provider* targets OpenAI's canonical models API."""
    if provider.family != OPENAI_FAMILY or not provider.base_url:
        return False
    from omnigent.onboarding.configure_models import default_base_url_for_family

    canonical = default_base_url_for_family(OPENAI_FAMILY)
    return _models_url(provider.base_url).lower() == _models_url(canonical).lower()


# Unfiltered listings keyed by provider identity. TTLCache is not thread-safe
# and enumeration runs via asyncio.to_thread, so accesses lock; the HTTP fetch
# stays outside it (duplicate fetches are benign, corruption is not).
_listing_cache: TTLCache[tuple[str, ...], ModelListing] = TTLCache(maxsize=64, ttl=_CATALOG_TTL_S)
_listing_cache_lock = threading.Lock()


def _credential_fingerprint(provider: ResolvedModelProvider) -> str:
    """Non-secret identity of the provider's credential for cache keying.

    Two providers sharing kind + base_url but holding different
    credentials may see different listings, so the cache key must carry
    credential identity without ever storing the secret itself.

    :param provider: The resolved provider descriptor.
    :returns: A sha256-prefix hex digest of the resolved credential (or
        ``auth_command`` string), e.g. ``"9f86d081884c7d65"``; ``""``
        when the provider carries no inline credential.
    """
    if provider.api_key:
        material = f"key:{provider.api_key}"
    elif provider.auth_command:
        material = f"cmd:{provider.auth_command}"
    else:
        return ""
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _listing_cache_key(provider: ResolvedModelProvider) -> tuple[str, ...]:
    """Cache identity for one provider's unfiltered listing.

    Carries the full provider coordinates — kind, family, profile,
    base URL, CLI, the non-secret ``detail`` (provider name), and a
    credential fingerprint — so distinct providers never replay each
    other's listings.

    :param provider: The resolved provider descriptor.
    :returns: A hashable tuple of non-secret identity strings.
    """
    return (
        provider.kind,
        provider.family or "",
        provider.profile or "",
        provider.base_url or "",
        provider.cli or "",
        provider.detail,
        _credential_fingerprint(provider),
    )


def clear_model_catalog_cache() -> None:
    """Drop every cached provider listing.

    Listings are cached per provider identity for
    :data:`_CATALOG_TTL_S`; call this after reconfiguring providers (or
    between tests) to force a fresh fetch.
    """
    with _listing_cache_lock:
        _listing_cache.clear()


def model_family_token(model_id: str) -> str:
    """Tag a model id with the harness family that can serve it.

    Shares the token rule with
    :func:`omnigent.model_override.model_family_mismatch`: Claude ids
    contain ``"claude"``; the ``"openai"`` token covers every
    codex-compatible id (gpt/codex plus the GLM and Kimi families, which
    serve on the same Responses wire).

    :param model_id: Model id, e.g. ``"databricks-claude-opus-4-8"``.
    :returns: ``"claude"``, ``"openai"``, or ``"other"``.
    """
    if "claude" in model_id.lower():
        return "claude"
    if is_codex_compatible_model(model_id):
        return "openai"
    return "other"


def catalog_model_entries(provider_name: str) -> tuple[ModelEntry, ...]:
    """Load provider chat models as normalized resolver candidates.

    The upstream MLflow catalog is fetched and cached by
    :func:`omnigent.onboarding.providers.get_chat_models`. This adapter keeps
    provider discovery separate from selection while preserving catalog order
    as the resolver's deterministic tie-breaker.

    :param provider_name: MLflow catalog provider, e.g. ``"openai"`` or
        ``"databricks"``.
    :returns: Normalized model entries, newest/preferred catalog entries first.
    """
    from omnigent.onboarding.providers import get_chat_models

    models = get_chat_models(provider_name)
    cost_tiers = _catalog_cost_tiers(models)
    entries: list[ModelEntry] = []
    for index, model in enumerate(models):
        supported: set[ModelCapability] = set()
        unsupported: set[ModelCapability] = set()
        for capability, value in (
            (ModelCapability.TOOL_USE, model.supports_function_calling),
            (ModelCapability.REASONING, model.supports_reasoning),
            (ModelCapability.VISION, model.supports_vision),
            (ModelCapability.STRUCTURED_OUTPUT, model.supports_structured_output),
        ):
            if value is True:
                supported.add(capability)
            elif value is False:
                unsupported.add(capability)
        entries.append(
            ModelEntry(
                id=model.name,
                family=model_family_token(model.name),
                metadata=ModelMetadata(
                    supported_capabilities=frozenset(supported),
                    unsupported_capabilities=frozenset(unsupported),
                    context_window=model.max_input_tokens,
                    max_output_tokens=model.max_output_tokens,
                    cost_tier=cost_tiers.get(index),
                ),
            )
        )
    return tuple(entries)


def resolve_catalog_model(
    provider_name: str,
    *,
    intent: ModelIntent = ModelIntent.DEFAULT,
    configured_default: str | None = None,
    family: str | None = None,
) -> ModelResolution:
    """Resolve a model from the live bundled provider catalog.

    Default intent preserves the onboarding provider's general-purpose model
    policy after family and gateway-routing constraints are applied. Other
    intents continue to rank all compatible catalog candidates by metadata.

    :param provider_name: MLflow catalog provider name.
    :param intent: Stable selection intent.
    :param configured_default: Optional provider-configured model. It is
        represented as a candidate in *family* so provider configuration keeps
        precedence even when the remote catalog has not learned the id yet.
    :param family: Optional normalized catalog family (``"claude"`` /
        ``"openai"`` / ``"other"``).
    :returns: The resolver result with source and normalized metadata.
    :raises ModelResolutionError: When no compatible catalog model exists.
    """
    from omnigent.onboarding.providers import default_chat_model

    models = list(catalog_model_entries(provider_name))
    if provider_name.lower() == "databricks":
        models = [model for model in models if model.id.lower().startswith("databricks-")]

    if configured_default is None and intent == ModelIntent.DEFAULT:
        compatible_ids = {model.id for model in models if family is None or model.family == family}
        preferred_default = default_chat_model(
            provider_name,
            allowed_models=compatible_ids,
        )
        if preferred_default is not None and (
            family is None or model_family_token(preferred_default) == family
        ):
            configured_default = preferred_default

    if configured_default is not None and all(model.id != configured_default for model in models):
        models.insert(
            0,
            ModelEntry(
                id=configured_default,
                family=family or model_family_token(configured_default),
            ),
        )
    try:
        return resolve_model(
            ModelResolutionRequest(
                intent=intent,
                configured_default=configured_default,
                allowed_families=(frozenset({family}) if family is not None else frozenset()),
            ),
            models,
        )
    except ModelResolutionError as exc:
        family_detail = f" for family {family!r}" if family is not None else ""
        raise ModelResolutionError(
            f"no compatible model resolved from provider {provider_name!r}{family_detail}; "
            "configure an explicit model or retry when catalog discovery is available"
        ) from exc


def _catalog_cost_tiers(models: list[ModelInfo]) -> dict[int, ModelCostTier]:
    """Assign provider-relative thirds from reported token prices."""
    priced: list[tuple[int, float]] = []
    for index, model in enumerate(models):
        input_price = getattr(model, "input_price", None)
        output_price = getattr(model, "output_price", None)
        if isinstance(input_price, (int, float)) and isinstance(output_price, (int, float)):
            priced.append((index, float(input_price) + float(output_price)))
    distinct = sorted({price for _, price in priced})
    if not distinct:
        return {}
    if len(distinct) == 1:
        return {index: ModelCostTier.STANDARD for index, _ in priced}
    tiers: dict[int, ModelCostTier] = {}
    price_rank = {price: rank / (len(distinct) - 1) for rank, price in enumerate(distinct)}
    for index, price in priced:
        percentile = price_rank[price]
        if percentile <= 1 / 3:
            tiers[index] = ModelCostTier.ECONOMY
        elif percentile >= 2 / 3:
            tiers[index] = ModelCostTier.PREMIUM
        else:
            tiers[index] = ModelCostTier.STANDARD
    return tiers


def spec_harness(spec: object) -> str | None:
    """Resolve the declared harness for a (sub-)agent spec.

    Mirrors the runner's harness derivation
    (``executor.config["harness"]`` falling back to ``executor.type``)
    with defensive attribute access so structural spec stubs degrade to
    ``None`` instead of raising.

    :param spec: An :class:`AgentSpec` (or structural equivalent).
    :returns: Harness id, e.g. ``"codex-native"``, or ``None``.
    """
    executor = getattr(spec, "executor", None)
    if executor is None:
        return None
    config = getattr(executor, "config", None)
    harness = config.get("harness") if isinstance(config, dict) else None
    if isinstance(harness, str) and harness:
        return harness
    executor_type = getattr(executor, "type", None)
    return executor_type if isinstance(executor_type, str) and executor_type else None


def resolve_model_provider(spec: object, harness: str | None) -> ResolvedModelProvider:
    """Resolve the model provider a worker's launch path would use.

    Total by contract: callers (the dispatch gate and ``sys_list_models``)
    must never crash on a malformed spec or broken provider config, so
    any resolution failure collapses to ``kind="none"`` — the gate then
    passes the model through unchanged and the tool reports the failure.

    :param spec: The worker's (sub-)agent spec.
    :param harness: The worker's harness id, e.g. ``"claude-native"``.
    :returns: A :class:`ResolvedModelProvider`; ``kind="none"`` when the
        provider cannot be determined.
    """
    try:
        return _resolve_model_provider_unsafe(spec, harness)
    except Exception as exc:  # noqa: BLE001 — total-function boundary: config/spec failures → "none"
        from omnigent.errors import OmnigentError

        _logger.debug("model provider resolution failed for harness %r", harness, exc_info=True)
        # OmnigentError text is this codebase's own (secret-free); anything
        # else is redacted to its type name — raw detail stays at DEBUG.
        reason = str(exc) if isinstance(exc, OmnigentError) else type(exc).__name__
        return ResolvedModelProvider(
            kind=NONE_KIND, detail=f"provider resolution failed: {reason}"
        )


def _resolve_model_provider_unsafe(spec: object, harness: str | None) -> ResolvedModelProvider:
    """Resolve the provider, propagating failures to the catch-all wrapper.

    Step 1 reuses :func:`~omnigent.runtime.workflow._resolve_provider_for_build`
    verbatim (the precedence the spawn-env builders and native launch
    paths share). Step 2 mirrors the builders' PER-HARNESS legacy
    fallthrough (see :func:`_provider_from_legacy_auth`) — the builders
    diverge in which legacy auth fields they actually consume.

    :param spec: The worker's (sub-)agent spec.
    :param harness: The worker's harness id, e.g. ``"pi"``.
    :returns: A :class:`ResolvedModelProvider`.
    """
    # Imported lazily; workflow.py imports broadly and this module is
    # consumed from the runner's dispatch path.
    from omnigent.runtime.workflow import _resolve_provider_for_build

    if (harness or "") in _CURSOR_HARNESSES:
        return ResolvedModelProvider(
            kind=SUBSCRIPTION_KIND, cli="cursor-agent", detail="cursor-agent CLI login"
        )

    harness_type = _PROVIDER_RESOLUTION_HARNESS.get(harness or "")
    if harness_type is None:
        return ResolvedModelProvider(
            kind=NONE_KIND,
            detail=f"harness {harness or 'unknown'!r} has no model-provider resolution",
        )

    agent_spec = cast("AgentSpec", spec)
    entry = _resolve_provider_for_build(agent_spec, harness_type=harness_type)
    if entry is not None:
        return _provider_from_entry(entry, harness_type)
    return _provider_from_legacy_auth(agent_spec, harness_type)


def _provider_from_legacy_auth(
    spec: AgentSpec, harness_type: _ProviderHarness
) -> ResolvedModelProvider:
    """Mirror the per-harness legacy fallthrough of ``_build_*_spawn_env``.

    The builders diverge: claude-sdk consumes spec/global ``auth:``
    blocks AND legacy profiles; openai-agents consumes ``auth:`` blocks
    and ``config["profile"]``; codex and pi consume ONLY
    ``config["profile"]`` plus the ``databricks-*`` model prefix — their
    builders never read ``auth:`` blocks, so reporting one as usable
    would list models the spawned child cannot actually reach.

    :param spec: The worker's (sub-)agent spec.
    :param harness_type: The workflow harness type, e.g. ``"codex"``.
    :returns: A :class:`ResolvedModelProvider`.
    """
    if harness_type == "claude-sdk":
        return _legacy_claude_sdk_provider(spec)
    if harness_type in ("openai-agents-sdk", "antigravity"):
        # Both resolve spec/global ``auth:`` api-key blocks via this branch.
        # NB: the antigravity spawn-env builder (unlike openai-agents) ignores
        # ``config["profile"]`` — it's Gemini-native with no Databricks/gateway
        # path — so for a profile-only antigravity spec this readout can
        # over-report; api-key (and Vertex) specs resolve correctly.
        return _legacy_openai_agents_provider(spec)
    return _legacy_profile_only_provider(spec, harness_type)


def _databricks_prefix_provider(spec: AgentSpec) -> ResolvedModelProvider | None:
    """Map a ``databricks-*`` spec model to the runner-env-profile gateway.

    Mirrors the builders' shared model-prefix heuristic; the native
    launch paths read the same ``DATABRICKS_CONFIG_PROFILE`` fallback.

    :param spec: The worker's (sub-)agent spec.
    :returns: A databricks provider, or ``None`` when the model carries
        no ``databricks-`` / ``databricks/`` prefix.
    """
    model = spec.executor.model
    if isinstance(model, str) and model.startswith(("databricks-", "databricks/")):
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND,
            profile=os.environ.get("DATABRICKS_CONFIG_PROFILE"),
            detail="databricks-* model prefix",
        )
    return None


def _legacy_claude_sdk_provider(spec: AgentSpec) -> ResolvedModelProvider:
    """Mirror ``_build_claude_sdk_spawn_env``'s legacy auth branch.

    Spec ``auth:`` (databricks / api_key) → legacy profile
    (``config["profile"]`` first, matching the builder's read order) →
    global ``auth:`` → ``databricks-*`` model prefix → none. The
    api_key path routes via ``apiKeyHelper`` to the vendor API, so
    ``auth.base_url`` is NOT consumed — listings use the vendor default.

    :param spec: The worker's (sub-)agent spec.
    :returns: A :class:`ResolvedModelProvider`.
    """
    from omnigent.onboarding.configure_models import default_base_url_for_family
    from omnigent.runtime.workflow import _load_global_auth
    from omnigent.spec.types import ApiKeyAuth, DatabricksAuth

    auth = spec.executor.auth
    legacy_profile = spec.executor.config.get("profile") or spec.executor.profile
    if auth is None and not legacy_profile:
        auth = _load_global_auth()
    if isinstance(auth, DatabricksAuth):
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND, profile=auth.profile or None, detail="databricks auth"
        )
    if isinstance(auth, ApiKeyAuth) and auth.api_key:
        return ResolvedModelProvider(
            kind=KEY_KIND,
            family=ANTHROPIC_FAMILY,
            base_url=default_base_url_for_family(ANTHROPIC_FAMILY),
            api_key=auth.api_key,
            detail="api_key auth",
        )
    if legacy_profile:
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND, profile=str(legacy_profile), detail="spec profile"
        )
    prefix = _databricks_prefix_provider(spec)
    if prefix is not None:
        return prefix
    return ResolvedModelProvider(kind=NONE_KIND, detail="no model provider configured")


def _legacy_openai_agents_provider(spec: AgentSpec) -> ResolvedModelProvider:
    """Mirror ``_build_openai_agents_sdk_spawn_env``'s legacy auth branch.

    Spec ``auth:`` (api_key with its base_url / databricks) → global
    ``auth:`` (only when the spec declares no auth or legacy profile) →
    ``config["profile"]`` → ``databricks-*`` model prefix → none.

    :param spec: The worker's (sub-)agent spec.
    :returns: A :class:`ResolvedModelProvider`.
    """
    from omnigent.onboarding.configure_models import default_base_url_for_family
    from omnigent.runtime.workflow import _load_global_auth
    from omnigent.spec.types import ApiKeyAuth, DatabricksAuth

    spec_auth = spec.executor.auth
    auth = spec_auth if isinstance(spec_auth, (ApiKeyAuth, DatabricksAuth)) else None
    has_legacy_profile = bool(spec.executor.profile or spec.executor.config.get("profile"))
    if auth is None and not has_legacy_profile:
        auth = _load_global_auth()
    if isinstance(auth, ApiKeyAuth) and auth.api_key:
        return ResolvedModelProvider(
            kind=KEY_KIND,
            family=OPENAI_FAMILY,
            base_url=auth.base_url or default_base_url_for_family(OPENAI_FAMILY),
            api_key=auth.api_key,
            detail="api_key auth",
        )
    if isinstance(auth, DatabricksAuth):
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND, profile=auth.profile or None, detail="databricks auth"
        )
    profile = spec.executor.config.get("profile")
    if profile:
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND, profile=str(profile), detail="spec profile"
        )
    prefix = _databricks_prefix_provider(spec)
    if prefix is not None:
        return prefix
    return ResolvedModelProvider(kind=NONE_KIND, detail="no model provider configured")


def _legacy_profile_only_provider(
    spec: AgentSpec, harness_type: _ProviderHarness
) -> ResolvedModelProvider:
    """Mirror the codex / pi builders' legacy branch (profile + prefix only).

    ``_build_codex_spawn_env`` / ``_build_pi_spawn_env`` never read
    ``auth:`` blocks or ``executor.profile`` — only ``config["profile"]``
    and the ``databricks-*`` model prefix route anywhere.

    :param spec: The worker's (sub-)agent spec.
    :param harness_type: The workflow harness type, e.g. ``"codex"``.
    :returns: A :class:`ResolvedModelProvider`.
    """
    profile = spec.executor.config.get("profile")
    if profile:
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND, profile=str(profile), detail="spec profile"
        )
    prefix = _databricks_prefix_provider(spec)
    if prefix is not None:
        return prefix
    if spec.executor.auth is not None or spec.executor.profile:
        return ResolvedModelProvider(
            kind=NONE_KIND,
            detail=(
                f"the {harness_type} spawn path does not consume legacy auth:/profile "
                "fields; configure a 'providers:' entry instead"
            ),
        )
    return ResolvedModelProvider(kind=NONE_KIND, detail="no model provider configured")


def _provider_from_entry(entry: ProviderEntry, harness_type: str) -> ResolvedModelProvider:
    """Map a resolved :class:`ProviderEntry` to a provider descriptor.

    :param entry: The provider entry resolved for the worker.
    :param harness_type: The workflow harness type, e.g. ``"codex"``.
    :returns: A :class:`ResolvedModelProvider`; ``kind="none"`` when an
        inline-family provider has no usable family for the harness.
    """
    from omnigent.errors import OmnigentError

    if entry.kind == DATABRICKS_KIND:
        return ResolvedModelProvider(
            kind=DATABRICKS_KIND, profile=entry.profile, detail=f"provider {entry.name!r}"
        )
    if entry.kind == SUBSCRIPTION_KIND:
        return ResolvedModelProvider(
            kind=SUBSCRIPTION_KIND, cli=entry.cli, detail=f"provider {entry.name!r}"
        )
    if entry.kind == CLI_CONFIG_KIND:
        # The provider table (base_url + credential) lives in the CLI's own
        # config file and the CLI resolves it at launch — there is nothing
        # to resolve statically here, so the entry is usable as-is. Falling
        # through to the inline-family loop would misreport it as "no
        # resolvable credentials" (cli-config entries carry no families).
        return ResolvedModelProvider(
            kind=CLI_CONFIG_KIND,
            cli=entry.cli,
            detail=(
                f"provider {entry.name!r} (codex config.toml model provider "
                f"{entry.model_provider!r})"
            ),
        )
    # Inline-family kinds: single-family harnesses get exactly their family;
    # pi takes the first whose credential resolves, anthropic preferred.
    preferred = _KEY_AUTH_FAMILY[harness_type] if harness_type != "pi" else None
    candidates = (preferred,) if preferred is not None else _FAMILY_PREFERENCE
    for family_name in candidates:
        try:
            family = entry.family(family_name)
        except OmnigentError:
            # Credential unset/unresolvable: skip (the pi optional-family rule).
            continue
        if family is None:
            continue
        return ResolvedModelProvider(
            kind=entry.kind,
            family=family_name,
            base_url=family.base_url,
            api_key=family.api_key,
            auth_command=family.auth_command,
            detail=f"provider {entry.name!r}",
        )
    return ResolvedModelProvider(
        kind=NONE_KIND,
        detail=(
            f"provider {entry.name!r} configures no family with resolvable "
            f"credentials for this harness"
        ),
    )


def list_models_for_worker(
    spec: object,
    harness: str | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ModelListing:
    """Enumerate the models one worker can run, family-filtered.

    Resolves the worker's provider, fetches (or replays from the TTL
    cache) its unfiltered model listing, then applies the harness's
    family rule from :func:`~omnigent.model_override.model_family_mismatch`
    — claude harnesses keep Claude ids, codex harnesses keep GPT ids,
    pi keeps everything.

    :param spec: The worker's (sub-)agent spec.
    :param harness: The worker's harness id, e.g. ``"codex-native"``.
    :param transport: Optional httpx transport override so tests mock at
        the HTTP boundary; ``None`` uses the default transport.
    :returns: The worker's :class:`ModelListing`.
    """
    provider = resolve_model_provider(spec, harness)
    # Pi harnesses use system.ai.* ids (via the Unity Catalog model-services API)
    # so supervisors see the ids Pi can actually route. Other harnesses use the
    # serving-endpoints listing which returns databricks-* ids.
    _pi_harnesses = frozenset({"pi", "pi-native", "native-pi"})
    canonical = (harness or "").lower().replace("-", "").replace("_", "")
    use_uc = canonical in {h.replace("-", "").replace("_", "") for h in _pi_harnesses}
    if use_uc and provider.kind == DATABRICKS_KIND:
        uc_key = ("uc", *_listing_cache_key(provider))
        with _listing_cache_lock:
            cached = cast(ModelListing | None, _listing_cache.get(uc_key))
        if cached is not None:
            listing = cached
        else:
            try:
                listing = _fetch_databricks_uc_listing(provider, transport=transport)
                with _listing_cache_lock:
                    _listing_cache[uc_key] = listing
            except (httpx.HTTPError, OSError):
                _logger.debug(
                    "UC model listing failed for pi harness, falling back", exc_info=True
                )
                listing = _listing_for_provider(provider, transport=transport)
    else:
        listing = _listing_for_provider(provider, transport=transport)
    if harness is None:
        return listing
    filtered = tuple(m for m in listing.models if model_family_mismatch(harness, m.id) is None)
    return replace(listing, models=filtered)


def catalog_for_spec(
    spec: object,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, _JsonObject]:
    """Build the full ``sys_list_models`` payload for an agent spec.

    One row per declared sub-agent, keyed by sub-agent name, plus a
    ``"self"`` row for the calling agent's own (brain) harness. Failures
    are isolated per worker: one broken provider yields a ``"none"`` row
    with the failure in its note and never hides the other workers.

    :param spec: The calling agent's spec (sub-agents enumerated from
        ``spec.sub_agents``).
    :param transport: Optional httpx transport override for tests.
    :returns: Mapping of worker name → row dict with ``source`` /
        ``verified`` / ``models`` / ``note`` keys.
    """
    rows: dict[str, _JsonObject] = {}
    for sub in getattr(spec, "sub_agents", None) or []:
        name = getattr(sub, "name", None)
        if not isinstance(name, str) or not name:
            continue
        rows[name] = _worker_row(sub, transport=transport)
    rows["self"] = _worker_row(spec, transport=transport)
    return rows


def _worker_row(
    spec: object,
    *,
    transport: httpx.BaseTransport | None,
) -> _JsonObject:
    """Build one worker's catalog row, never raising.

    :param spec: The worker's (sub-)agent spec.
    :param transport: Optional httpx transport override for tests.
    :returns: Row dict with ``source`` / ``verified`` / ``models`` /
        ``note`` keys.
    """
    harness = spec_harness(spec)
    try:
        listing = list_models_for_worker(spec, harness, transport=transport)
    except Exception as exc:  # noqa: BLE001 — per-worker isolation: fail informative, never crash the tool
        _logger.debug("worker model enumeration failed", exc_info=True)
        listing = ModelListing(
            source=NONE_KIND,
            verified=False,
            models=(),
            note=f"model enumeration failed: {_redacted_failure_reason(exc)}",
        )
    return _listing_payload(listing)


def _listing_payload(listing: ModelListing) -> _JsonObject:
    """Serialize a :class:`ModelListing` into the tool's JSON row shape.

    :param listing: The listing to serialize.
    :returns: Row dict; ``context_window`` appears only when known.
    """
    models: list[_JsonObject] = []
    for entry in listing.models:
        row: _JsonObject = {"id": entry.id, "family": entry.family}
        metadata = entry.metadata
        if metadata.context_window is not None:
            row["context_window"] = metadata.context_window
        if metadata.max_output_tokens is not None:
            row["max_output_tokens"] = metadata.max_output_tokens
        capabilities = {
            capability.value: supported
            for supported, values in (
                (True, metadata.supported_capabilities),
                (False, metadata.unsupported_capabilities),
            )
            for capability in sorted(values, key=lambda value: value.value)
        }
        if capabilities:
            row["capabilities"] = capabilities
        if metadata.cost_tier is not None:
            row["cost_tier"] = metadata.cost_tier.value
        if metadata.wire_apis:
            row["wire_apis"] = sorted(wire_api.value for wire_api in metadata.wire_apis)
        if metadata.reasoning is not None:
            row["reasoning"] = {
                "modes": sorted(mode.value for mode in metadata.reasoning.modes),
                "efforts": sorted(metadata.reasoning.efforts),
            }
        models.append(row)
    payload: _JsonObject = {
        "source": listing.source,
        "verified": listing.verified,
        "models": models,
        "note": listing.note,
    }
    return payload


def _redacted_failure_reason(exc: Exception) -> str:
    """Map an enumeration failure to a secret-free note category.

    Raw exception text can embed secrets — ``CalledProcessError`` /
    ``TimeoutExpired`` stringify the full ``auth_command`` — and the
    note flows into ``sys_list_models`` output (LLM-visible, persisted
    in the transcript). Callers log the raw exception at DEBUG.

    :param exc: The enumeration failure.
    :returns: A redacted human-readable category, e.g.
        ``"listing endpoint returned HTTP 503"``.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return "provider auth command timed out"
    if isinstance(exc, subprocess.SubprocessError):
        return "provider auth command failed"
    if isinstance(exc, click.ClickException):
        return exc.message
    if isinstance(exc, httpx.HTTPStatusError):
        return f"listing endpoint returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "listing endpoint unreachable"
    if isinstance(exc, json.JSONDecodeError):
        return "listing endpoint returned malformed JSON"
    if isinstance(exc, ValueError):
        # The remaining ValueErrors are this module's own static,
        # secret-free messages (no credential / no base_url / empty token).
        return str(exc)
    if isinstance(exc, OSError):
        return "provider credentials or network unavailable"
    return type(exc).__name__


def _listing_for_provider(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """Enumerate (or replay from cache) one provider's unfiltered listing.

    Live fetches are cached for :data:`_CATALOG_TTL_S` keyed by provider
    identity; failures are returned (not cached) so a transient outage
    retries on the next call.

    :param provider: The resolved provider descriptor.
    :param transport: Optional httpx transport override for tests.
    :returns: The provider's :class:`ModelListing`.
    """
    if provider.kind == NONE_KIND:
        return ModelListing(
            source=NONE_KIND,
            verified=False,
            models=(),
            note=(
                f"no usable model provider ({provider.detail}) — dispatches to "
                "this worker cannot run here"
            ),
        )
    if provider.kind == SUBSCRIPTION_KIND and provider.cli != "cursor-agent":
        return _static_subscription_listing(provider)
    if provider.kind == CLI_CONFIG_KIND:
        return _static_cli_config_listing(provider)

    cache_key = _listing_cache_key(provider)
    with _listing_cache_lock:
        cached = cast(ModelListing | None, _listing_cache.get(cache_key))
    if cached is not None:
        return cached
    try:
        if provider.kind == SUBSCRIPTION_KIND:
            listing = _fetch_cursor_cli_listing(provider)
        elif provider.kind == DATABRICKS_KIND:
            listing = _fetch_databricks_listing(provider, transport=transport)
        elif provider.kind == KEY_KIND and provider.family == ANTHROPIC_FAMILY:
            listing = _fetch_anthropic_listing(provider, transport=transport)
        else:
            listing = _fetch_openai_compatible_listing(provider, transport=transport)
    except (
        click.ClickException,
        httpx.HTTPError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        _logger.debug(
            "model enumeration failed for %s", provider.detail or provider.kind, exc_info=True
        )
        return ModelListing(
            source=NONE_KIND,
            verified=False,
            models=(),
            note=(
                f"model enumeration failed for {provider.detail or provider.kind}: "
                f"{_redacted_failure_reason(exc)}"
            ),
        )
    with _listing_cache_lock:
        _listing_cache[cache_key] = listing
    return listing


def _fetch_cursor_cli_listing(provider: ResolvedModelProvider) -> ModelListing:
    """Build a live listing from the installed Cursor CLI."""
    from omnigent.cursor_native import list_cursor_cli_model_options

    options = list_cursor_cli_model_options()
    return ModelListing(
        source="cli",
        verified=True,
        models=tuple(
            ModelEntry(id=str(option["id"]), family=model_family_token(str(option["id"])))
            for option in options
        ),
        note=f"live models advertised by the {provider.cli or 'cursor-agent'} CLI",
    )


def _static_subscription_listing(provider: ResolvedModelProvider) -> ModelListing:
    """Build the (empty) pre-launch listing for a subscription CLI login.

    Subscription logins expose no model-listing API, and the curated
    stand-ins this used to serve are gone — the live harness probes are the
    source of truth, so a path that cannot probe reports nothing rather
    than a plausible-but-stale list.

    :param provider: A ``kind="subscription"`` provider descriptor.
    :returns: A ``source="static"`` listing with no models.
    """
    return ModelListing(
        source="static",
        verified=False,
        models=(),
        note=(
            f"the {provider.cli or 'unknown'} CLI login exposes no model-listing "
            "API before launch; the live listing comes from probing the harness"
        ),
    )


def _static_cli_config_listing(provider: ResolvedModelProvider) -> ModelListing:
    """Build the curated static listing for a ``cli-config`` provider.

    A ``cli-config`` provider pins a custom ``[model_providers.X]`` table in
    the codex CLI's own ``config.toml``; its credential (an auth command /
    env key in that file) is resolved by codex at launch, so the listing is
    the codex curated ids with a note saying the credential is the CLI's to
    resolve — not a "no credentials" preflight failure.

    :param provider: A ``kind="cli-config"`` provider descriptor.
    :returns: A ``source="static"`` listing with no models.
    """
    return ModelListing(
        source="static",
        verified=False,
        models=(),
        note=(
            f"{provider.detail} enumerates its models only from the CLI's own "
            "config at launch; the live listing comes from probing the harness"
        ),
    )


def _is_llm_endpoint(name: str, task: str) -> bool:
    """Decide whether a serving endpoint is a chat-capable LLM.

    :param name: Endpoint name, e.g. ``"databricks-claude-opus-4-8"``.
    :param task: Endpoint ``task`` field, e.g. ``"llm/v1/chat"`` —
        empty when the API omits it.
    :returns: ``True`` for chat-capable LLM endpoints; embeddings and
        other non-chat tasks are excluded.
    """
    task_lower = task.lower()
    if task_lower:
        # An explicit task is authoritative: only chat/completions
        # endpoints qualify (embeddings carry "llm/v1/embeddings").
        return any(token in task_lower for token in _LLM_TASK_TOKENS)
    name_lower = name.lower()
    return any(token in name_lower for token in _LLM_NAME_TOKENS)


def _fetch_databricks_listing(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """List LLM serving endpoints on the provider's Databricks workspace.

    :param provider: A ``kind="databricks"`` provider descriptor.
    :param transport: Optional httpx transport override for tests.
    :returns: A ``source="gateway"`` listing of LLM endpoint names.
    :raises httpx.HTTPError: On transport/HTTP failures.
    :raises OSError: When the profile resolves no credentials.
    """
    creds = resolve_databricks_workspace(provider.profile)
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(
            f"{creds.host}/api/2.0/serving-endpoints",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
    endpoints = payload.get("endpoints") if isinstance(payload, dict) else None
    models: list[ModelEntry] = []
    for endpoint in endpoints if isinstance(endpoints, list) else []:
        if not isinstance(endpoint, dict):
            continue
        name = endpoint.get("name")
        if not isinstance(name, str) or not name:
            continue
        task = endpoint.get("task")
        if not _is_llm_endpoint(name, task if isinstance(task, str) else ""):
            continue
        state = endpoint.get("state")
        ready = state.get("ready") if isinstance(state, dict) else None
        # Only an explicitly non-READY endpoint is skipped; an absent
        # state field stays included (the API may omit it).
        if isinstance(ready, str) and ready and ready.upper() != "READY":
            continue
        models.append(ModelEntry(id=name, family=model_family_token(name)))
    return ModelListing(
        source="gateway",
        verified=True,
        models=tuple(models),
        note=(
            "LLM serving endpoints on the Databricks workspace gateway "
            f"(profile {provider.profile or 'DEFAULT'!r})"
        ),
    )


def _fetch_databricks_uc_listing(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """List LLM model services via the Unity Catalog model-services API.

    Returns ``system.ai.*`` model ids directly — the ids that work with the
    AI Gateway — avoiding the ``databricks-*`` → ``system.ai.*`` translation.

    :param provider: A ``kind="databricks"`` provider descriptor.
    :param transport: Optional httpx transport override for tests.
    :returns: A ``source="gateway"`` listing of model service names.
    :raises httpx.HTTPError: On transport/HTTP failures.
    :raises OSError: When the profile resolves no credentials.
    """
    creds = resolve_databricks_workspace(provider.profile)
    models = tuple(
        model
        for model in fetch_databricks_model_service_entries(
            creds.host,
            creds.token,
            transport=transport,
        )
        if not unsupported_in_pi(model.id.lower())
    )
    return ModelListing(
        source="gateway",
        verified=True,
        models=models,
        note=(
            "LLM model services on the Databricks workspace "
            f"(profile {provider.profile or 'DEFAULT'!r})"
        ),
    )


def fetch_databricks_model_service_entries(
    workspace_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[ModelEntry, ...]:
    """Fetch normalized Unity Catalog model-service metadata.

    This is the shared parser for worker model enumeration and Pi gateway
    configuration. It reports provider wire surfaces without deciding which
    one a harness should prefer.

    :param workspace_url: Databricks workspace base URL.
    :param token: Workspace bearer token.
    :param transport: Optional httpx transport override for tests.
    :returns: LLM model-service entries with normalized wire metadata.
    :raises httpx.HTTPError: On transport or HTTP failures.
    """
    # DATABRICKS-PATCH(model-services-scoped-listing)
    # The listing must be scoped to the `system.ai` schema and paged. Unscoped,
    # the endpoint walks the WHOLE metastore and returns one page of whatever
    # schemas sort first — on a busy workspace that is a slice of unrelated user
    # schemas with a `next_page_token` this call never followed, so a workspace
    # serving 53 Databricks models reported 2 and zero Claude entries. Same
    # scoping `databricks_model_discovery._list_model_service_ids` already uses.
    services: list[object] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        for _ in range(_MODEL_SERVICES_MAX_PAGES):
            params = {
                "parent": _MODEL_SERVICES_PARENT,
                "max_results": str(_MODEL_SERVICES_MAX_RESULTS),
            }
            if page_token is not None:
                params["page_token"] = page_token
            resp = client.get(
                f"{workspace_url.rstrip('/')}/api/2.1/unity-catalog/model-services",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            payload = resp.json()
            page = payload.get("model_services") if isinstance(payload, dict) else None
            if isinstance(page, list):
                services.extend(page)
            raw_next = payload.get("next_page_token") if isinstance(payload, dict) else None
            if not isinstance(raw_next, str) or not raw_next:
                break
            if raw_next in seen_tokens:
                # A repeated token means the endpoint is looping. Keep the pages
                # already collected instead of raising (which is what the
                # sibling ``_list_model_service_ids`` does): every caller here
                # treats an exception as "no listing" and falls back to the
                # bundled catalog's retired ``databricks-`` ids, so failing loud
                # would reintroduce the 501 this patch exists to remove. A
                # partial ``system.ai`` list still launches.
                _logger.warning(
                    "Databricks model-services pagination repeated a page token; "
                    "returning the %d entries collected so far",
                    len(services),
                )
                break
            seen_tokens.add(raw_next)
            page_token = raw_next
        else:
            _logger.warning(
                "Databricks model-services listing truncated after %d pages; "
                "the model list may be incomplete",
                _MODEL_SERVICES_MAX_PAGES,
            )
    models: list[ModelEntry] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        raw_name = service.get("name")
        if not isinstance(raw_name, str):
            continue
        name = (
            raw_name.replace("model-services/", "")
            if raw_name.startswith("model-services/")
            else raw_name
        )
        if not name:
            continue
        api_types = service.get("supported_api_types")
        normalized_api_types = {
            api_type.lower()
            for api_type in (api_types if isinstance(api_types, list) else [])
            if isinstance(api_type, str)
        }
        wire_apis: set[ModelWireAPI] = set()
        if any("chat/completions" in api_type for api_type in normalized_api_types):
            wire_apis.add(ModelWireAPI.OPENAI_CHAT)
        if "openai/v1/responses" in normalized_api_types:
            wire_apis.add(ModelWireAPI.OPENAI_RESPONSES)
        if any(
            "anthropic" in api_type and "messages" in api_type for api_type in normalized_api_types
        ):
            wire_apis.add(ModelWireAPI.ANTHROPIC_MESSAGES)
        if not wire_apis:
            continue
        models.append(
            ModelEntry(
                id=name,
                family=model_family_token(name),
                metadata=ModelMetadata(wire_apis=frozenset(wire_apis)),
            )
        )
    return tuple(models)


def _models_url(base_url: str) -> str:
    """Derive the model-listing URL from a provider base URL.

    :param base_url: Endpoint base URL, e.g.
        ``"https://openrouter.ai/api/v1"`` or
        ``"https://api.anthropic.com"``.
    :returns: The listing URL — ``<base>/models`` when the base already
        ends in ``/v1``, else ``<base>/v1/models``.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return f"{trimmed}/models"
    return f"{trimmed}/v1/models"


def _resolve_bearer_token(provider: ResolvedModelProvider) -> str:
    """Resolve the provider's credential to a bearer-token string.

    :param provider: An inline-family provider descriptor.
    :returns: The token, e.g. ``"sk-or-..."``.
    :raises ValueError: When the provider carries no credential or its
        ``auth_command`` prints nothing.
    :raises subprocess.SubprocessError: When the ``auth_command`` fails.
    """
    if provider.api_key:
        return provider.api_key
    if provider.auth_command:
        # Same trust model as the harness executors, which run the
        # user-configured auth_command to mint gateway tokens.
        result = subprocess.run(
            default_shell_argv(provider.auth_command),
            capture_output=True,
            text=True,
            timeout=_AUTH_COMMAND_TIMEOUT_S,
            check=True,
        )
        token = result.stdout.strip()
        if not token:
            raise ValueError("provider auth_command printed no token")
        return token
    raise ValueError("provider has no credential to list models with")


def _fetch_openai_compatible_listing(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """List models from an OpenAI-compatible ``/v1/models`` endpoint.

    :param provider: An inline-family provider descriptor (an OpenAI key
        or an OpenRouter/LiteLLM-style gateway/local endpoint).
    :param transport: Optional httpx transport override for tests.
    :returns: A ``source="openai-compatible"`` listing; entries carry
        ``context_window`` when the endpoint reports ``context_length``.
    :raises ValueError: When the provider has no base URL or credential.
    :raises httpx.HTTPError: On transport/HTTP failures.
    """
    if not provider.base_url:
        raise ValueError("provider has no base_url to list models from")
    token = _resolve_bearer_token(provider)
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(
            _models_url(provider.base_url),
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
    models: list[ModelEntry] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        context_length = item.get("context_length")
        models.append(
            ModelEntry(
                id=model_id,
                family=model_family_token(model_id),
                metadata=ModelMetadata(
                    context_window=context_length if isinstance(context_length, int) else None
                ),
            )
        )
    return ModelListing(
        source="openai-compatible",
        verified=True,
        models=tuple(models),
        note=f"models reported by {_models_url(provider.base_url)}",
    )


def _fetch_anthropic_listing(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """List models from the Anthropic models API (real keys only).

    :param provider: A ``kind="key"`` anthropic-family descriptor.
    :param transport: Optional httpx transport override for tests.
    :returns: A ``source="anthropic-api"`` listing.
    :raises ValueError: When the provider has no base URL or credential.
    :raises httpx.HTTPError: On transport/HTTP failures (subscription
        OAuth tokens are rejected here — only real API keys work).
    """
    if not provider.base_url:
        raise ValueError("provider has no base_url to list models from")
    token = _resolve_bearer_token(provider)
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(
            _models_url(provider.base_url),
            headers={"x-api-key": token, "anthropic-version": _ANTHROPIC_API_VERSION},
        )
        resp.raise_for_status()
        payload = resp.json()
    models: list[ModelEntry] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        models.append(
            ModelEntry(
                id=model_id,
                family=model_family_token(model_id),
                metadata=parse_anthropic_model_metadata(item),
            )
        )
    return ModelListing(
        source="anthropic-api",
        verified=True,
        models=tuple(models),
        note=f"models reported by {_models_url(provider.base_url)}",
    )
