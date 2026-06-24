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
- ``subscription`` → a curated static list (source ``"static"``,
  ``verified: false`` — CLI logins expose no listing API).
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
from dataclasses import dataclass, replace
from typing import Any

import httpx
from cachetools import TTLCache

from omnigent.model_override import model_family_mismatch
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    DATABRICKS_KIND,
    KEY_KIND,
    OPENAI_FAMILY,
    SUBSCRIPTION_KIND,
    ProviderEntry,
)
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

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

# Subscription CLIs expose no listing API: curated ids matching the bundled
# catalog pin (claude) and the codex ids the codebase already references.
_SUBSCRIPTION_STATIC_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"),
    "codex": ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"),
    "cursor": (
        "auto",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "gemini-3.1-pro",
        "composer-2.5",
    ),
}

# Harness spellings -> the workflow harness whose provider resolution they
# share; natives resolve via their SDK sibling (the resolve_native_* rule).
_PROVIDER_RESOLUTION_HARNESS: dict[str, str] = {
    "claude-sdk": "claude-sdk",
    "claude_sdk": "claude-sdk",
    "claude": "claude-sdk",
    "claude-native": "claude-sdk",
    "native-claude": "claude-sdk",
    "codex": "codex",
    "codex-native": "codex",
    "native-codex": "codex",
    "cursor": "cursor",
    "cursor-native": "cursor",
    "native-cursor": "cursor",
    "pi": "pi",
    "pi-native": "pi",
    "native-pi": "pi",
    "openai-agents": "openai-agents-sdk",
    "openai-agents-sdk": "openai-agents-sdk",
    "agents_sdk": "openai-agents-sdk",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "google-antigravity": "antigravity",
}

# Preferred inline family per single-family harness (pi consumes both).
_KEY_AUTH_FAMILY: dict[str, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
    "codex": OPENAI_FAMILY,
    "cursor": OPENAI_FAMILY,
    "openai-agents-sdk": OPENAI_FAMILY,
    "antigravity": OPENAI_FAMILY,
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
    :param context_window: Context window in tokens when the provider
        reports one (e.g. OpenRouter ``context_length``), else ``None``.
    """

    id: str
    family: str
    context_window: int | None = None


@dataclass(frozen=True)
class ModelListing:
    """A worker's enumerated model list plus its provenance.

    :param source: Where the list came from — ``"gateway"``,
        ``"openai-compatible"``, ``"anthropic-api"``, ``"static"``, or
        ``"none"``.
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
        / ``"subscription"`` / ``"databricks"`` from the provider config
        layer, or ``"none"`` when no usable provider resolved.
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
    :param cli: ``"claude"`` / ``"codex"`` for ``kind="subscription"``.
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
    """Tag a model id with its vendor family.

    Mirrors the token rule in
    :func:`omnigent.model_override.model_family_mismatch`: Claude ids
    contain ``"claude"``; GPT ids contain ``"gpt"`` or ``"codex"``.

    :param model_id: Model id, e.g. ``"databricks-claude-opus-4-8"``.
    :returns: ``"claude"``, ``"openai"``, or ``"other"``.
    """
    lower = model_id.lower()
    if "claude" in lower:
        return "claude"
    if "gpt" in lower or "codex" in lower:
        return "openai"
    return "other"


def spec_harness(spec: Any) -> str | None:  # type: ignore[explicit-any]  # structural spec stubs in tests
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


def resolve_model_provider(spec: Any, harness: str | None) -> ResolvedModelProvider:  # type: ignore[explicit-any]  # structural spec stubs in tests
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


def _resolve_model_provider_unsafe(spec: Any, harness: str | None) -> ResolvedModelProvider:  # type: ignore[explicit-any]  # structural spec stubs in tests
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

    harness_type = _PROVIDER_RESOLUTION_HARNESS.get(harness or "")
    if harness_type is None:
        return ResolvedModelProvider(
            kind=NONE_KIND,
            detail=f"harness {harness or 'unknown'!r} has no model-provider resolution",
        )

    entry = _resolve_provider_for_build(spec, harness_type=harness_type)  # type: ignore[arg-type]  # AgentHarnessType narrowed by the map above
    if entry is not None:
        return _provider_from_entry(entry, harness_type)
    return _provider_from_legacy_auth(spec, harness_type)


def _provider_from_legacy_auth(spec: Any, harness_type: str) -> ResolvedModelProvider:  # type: ignore[explicit-any]  # structural spec stubs in tests
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


def _databricks_prefix_provider(spec: Any) -> ResolvedModelProvider | None:  # type: ignore[explicit-any]  # structural spec stubs in tests
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


def _legacy_claude_sdk_provider(spec: Any) -> ResolvedModelProvider:  # type: ignore[explicit-any]  # structural spec stubs in tests
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


def _legacy_openai_agents_provider(spec: Any) -> ResolvedModelProvider:  # type: ignore[explicit-any]  # structural spec stubs in tests
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


def _legacy_profile_only_provider(spec: Any, harness_type: str) -> ResolvedModelProvider:  # type: ignore[explicit-any]  # structural spec stubs in tests
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
    spec: Any,  # type: ignore[explicit-any]  # structural spec stubs in tests
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
    listing = _listing_for_provider(provider, transport=transport)
    if harness is None:
        return listing
    filtered = tuple(m for m in listing.models if model_family_mismatch(harness, m.id) is None)
    return replace(listing, models=filtered)


def catalog_for_spec(
    spec: Any,  # type: ignore[explicit-any]  # structural spec stubs in tests
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, dict[str, Any]]:  # type: ignore[explicit-any]  # JSON-shaped tool payload
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
    rows: dict[str, dict[str, Any]] = {}  # type: ignore[explicit-any]  # JSON-shaped tool payload
    for sub in getattr(spec, "sub_agents", None) or []:
        name = getattr(sub, "name", None)
        if not isinstance(name, str) or not name:
            continue
        rows[name] = _worker_row(sub, transport=transport)
    rows["self"] = _worker_row(spec, transport=transport)
    return rows


def _worker_row(
    spec: Any,  # type: ignore[explicit-any]  # structural spec stubs in tests
    *,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON-shaped tool payload
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


def _listing_payload(listing: ModelListing) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON-shaped tool payload
    """Serialize a :class:`ModelListing` into the tool's JSON row shape.

    :param listing: The listing to serialize.
    :returns: Row dict; ``context_window`` appears only when known.
    """
    models: list[dict[str, Any]] = []  # type: ignore[explicit-any]  # JSON-shaped tool payload
    for entry in listing.models:
        row: dict[str, Any] = {"id": entry.id, "family": entry.family}  # type: ignore[explicit-any]
        if entry.context_window is not None:
            row["context_window"] = entry.context_window
        models.append(row)
    return {
        "source": listing.source,
        "verified": listing.verified,
        "models": models,
        "note": listing.note,
    }


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
    if provider.kind == SUBSCRIPTION_KIND:
        return _static_subscription_listing(provider)

    cache_key = _listing_cache_key(provider)
    with _listing_cache_lock:
        cached = _listing_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        if provider.kind == DATABRICKS_KIND:
            listing = _fetch_databricks_listing(provider, transport=transport)
        elif provider.kind == KEY_KIND and provider.family == ANTHROPIC_FAMILY:
            listing = _fetch_anthropic_listing(provider, transport=transport)
        else:
            listing = _fetch_openai_compatible_listing(provider, transport=transport)
    except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError) as exc:
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


def _static_subscription_listing(provider: ResolvedModelProvider) -> ModelListing:
    """Build the curated static listing for a subscription CLI login.

    :param provider: A ``kind="subscription"`` provider descriptor.
    :returns: A ``source="static"`` listing with ``verified=False``.
    """
    ids = _SUBSCRIPTION_STATIC_MODELS.get(provider.cli or "", ())
    return ModelListing(
        source="static",
        verified=False,
        models=tuple(ModelEntry(id=i, family=model_family_token(i)) for i in ids),
        note=(
            f"curated aliases for the {provider.cli or 'unknown'} CLI login "
            "(subscription logins expose no model-listing API; availability "
            "depends on the logged-in plan)"
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
            ["/bin/sh", "-c", provider.auth_command],
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
                context_window=context_length if isinstance(context_length, int) else None,
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
        models.append(ModelEntry(id=model_id, family=model_family_token(model_id)))
    return ModelListing(
        source="anthropic-api",
        verified=True,
        models=tuple(models),
        note=f"models reported by {_models_url(provider.base_url)}",
    )
