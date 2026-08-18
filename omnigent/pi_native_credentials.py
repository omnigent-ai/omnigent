"""Translate the omnigent-configured model provider into native Pi config.

A native Pi session launches the ``pi`` CLI, which authenticates from its own
config directory (``~/.pi/agent``). Without help, a user who ran ``omnigent
setup`` would still have to run ``pi`` ``/login`` separately — unlike
claude-native / codex-native, which route through the provider that ``omnigent
setup`` configured.

This module closes that gap. It resolves the provider configured for the Pi
surface (``~/.omnigent/config.yaml``) and writes a per-session ``models.json``
into a *managed* Pi config dir (selected via ``PI_CODING_AGENT_DIR``), so the
runner-owned ``pi`` process authenticates exactly like the configured harness —
mirroring how codex-native routes through the Databricks AI Gateway.

The managed config dir is per-session (like codex-native's managed
``CODEX_HOME``), so this never mutates the user's global ``~/.pi/agent``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypeAlias, TypedDict, TypeGuard
from urllib.parse import urlparse

from omnigent import model_catalog
from omnigent.databricks_ai_gateway import (
    DATABRICKS_AI_GATEWAY_LABEL,
    DATABRICKS_TRUSTED_HOST_SUFFIXES,
    is_databricks_ai_gateway_url,
)
from omnigent.model_metadata import ModelWireAPI
from omnigent.model_override import normalize_model_for_provider
from omnigent.onboarding.provider_config import (
    CHAT_WIRE_API,
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LOCAL_KIND,
    PI_SURFACE,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
)
from omnigent.pi_model_compatibility import (
    SYSTEM_AI_RESPONSES_KEYWORDS,
    DatabricksPiSurface,
    PiModelEntry,
    databricks_pi_surface_for_model,
    enrich_databricks_model_catalog,
    pi_model_json_entry,
    unsupported_in_pi,
)
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

if TYPE_CHECKING:
    # Annotation-only import (the runtime import is lazy inside the function,
    # since ``ambient`` pulls in onboarding-only deps this module avoids on the
    # runner's session-create hot path).
    from omnigent.onboarding.ambient import CodexConfigTransport

_LOGGER = logging.getLogger(__name__)

# Env var the ``pi`` CLI reads to relocate its config dir (default
# ``~/.pi/agent``). Setting it per session gives Pi a managed, isolated
# config dir we own — the analog of codex-native's ``CODEX_HOME``.
PI_CODING_AGENT_DIR_ENV_VAR = "PI_CODING_AGENT_DIR"

# Provider id registered in the generated ``models.json``. Stable so
# ``--provider`` can select it.
_PI_PROVIDER_ID = "omnigent"

# Provider id for the secondary OpenAI Responses provider (GPT models that only
# support tools via the Responses API, e.g. gpt-5.5, gpt-5.6-*).
_PI_OPENAI_PROVIDER_ID = "omnigent-openai"

# Provider id for the tertiary OpenAI Completions provider (non-GPT models that
# work via /chat/completions: Kimi, Llama, GLM, Gemini, older GPT models).
_PI_COMPLETIONS_PROVIDER_ID = "omnigent-completions"
_PI_MLFLOW_PROVIDER_ID = "omnigent-mlflow"

# Which provider id serves each Databricks gateway surface. The Anthropic
# surface is the primary provider, so it is registered inline, not here.
_SURFACE_PROVIDER_IDS: dict[DatabricksPiSurface, str] = {
    DatabricksPiSurface.RESPONSES: _PI_OPENAI_PROVIDER_ID,
    DatabricksPiSurface.COMPLETIONS: _PI_COMPLETIONS_PROVIDER_ID,
    DatabricksPiSurface.MLFLOW: _PI_MLFLOW_PROVIDER_ID,
}

# Databricks AI Gateway Anthropic Messages surface. Pi speaks this protocol
# natively (``api: anthropic-messages``); the gateway authenticates with a
# workspace bearer token, so we set ``authHeader`` (Authorization: Bearer).
_DATABRICKS_ANTHROPIC_GATEWAY_PATH = "/ai-gateway/anthropic"

# The Databricks AI Gateway exposes one surface per protocol under the same
# workspace origin: Codex/OpenAI-Responses at ``/codex/v1`` and Anthropic
# Messages at ``/anthropic``. ``isaac configure codex`` writes the Codex
# base_url; pi-native rewrites it to the Anthropic surface Pi speaks natively.
_DATABRICKS_GATEWAY_CODEX_SUFFIX = "/codex/v1"
_DATABRICKS_GATEWAY_ANTHROPIC_SUFFIX = "/anthropic"

# Aliases for the canonical Databricks AI Gateway predicate and its constants,
# which live in :mod:`omnigent.databricks_ai_gateway` so every surface that must
# recognize the gateway agrees.
_DATABRICKS_TRUSTED_HOST_SUFFIXES = DATABRICKS_TRUSTED_HOST_SUFFIXES
_DATABRICKS_AI_GATEWAY_LABEL = DATABRICKS_AI_GATEWAY_LABEL
_is_databricks_ai_gateway_url = is_databricks_ai_gateway_url


# Declared in pi_model_compatibility so the harness and interactive paths
# render byte-identical entries.
_PiModelEntry: TypeAlias = PiModelEntry


class _PiProviderCompat(TypedDict):
    supportsDeveloperRole: bool
    supportsStore: bool
    supportsStrictMode: bool
    supportsReasoningEffort: bool
    supportsUsageInStreaming: bool


class _PiProviderPayload(TypedDict):
    baseUrl: str
    apiKey: str
    api: str
    models: list[_PiModelEntry]
    authHeader: NotRequired[bool]
    compat: NotRequired[_PiProviderCompat]


class _PiModelsConfig(TypedDict):
    providers: dict[str, _PiProviderPayload]


_PiModelLists: TypeAlias = tuple[
    list[_PiModelEntry],
    list[_PiModelEntry],
    list[_PiModelEntry],
    list[_PiModelEntry],
]


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _databricks_workspace_url_for_gateway(
    base_url: str,
    *,
    profile: str | None = None,
) -> str | None:
    """Resolve the workspace API origin behind a Databricks AI Gateway URL.

    Workspace-hosted gateways already expose the workspace hostname. Dedicated
    ``ai-gateway`` subdomains require the configured Databricks profile because
    the gateway hostname itself does not serve workspace APIs.

    :param base_url: Gateway protocol URL or origin.
    :param profile: Optional Databricks profile for dedicated gateway hosts.
    :returns: Workspace origin, or ``None`` for non-Databricks/unresolved URLs.
    """
    if not _is_databricks_ai_gateway_url(base_url):
        return None
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    if hostname is None:
        return None
    if _DATABRICKS_AI_GATEWAY_LABEL not in hostname.lower().split("."):
        return f"https://{hostname}"
    try:
        return resolve_databricks_workspace(profile).host
    except Exception:  # noqa: BLE001 — absent profile disables optional discovery
        return None


@dataclass(frozen=True)
class PiProviderConfig:
    """A resolved native-Pi provider, ready to render into ``models.json``.

    :param provider_id: Provider id used in ``models.json`` and ``--provider``.
    :param base_url: Endpoint base URL the ``pi`` CLI talks to.
    :param api: Pi API type, e.g. ``"anthropic-messages"`` or
        ``"openai-responses"``.
    :param model: Model id to select, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param api_key: Credential value for ``models.json`` ``apiKey`` — a literal
        key, an env-var name, or a ``"!command"`` shell form (resolved by Pi at
        request time, used for short-lived gateway tokens).
    :param auth_header: When ``True``, Pi sends ``Authorization: Bearer
        <apiKey>`` (gateways) instead of a provider-native key header.
    :param credential_warning: A user-facing notice set when the provider was
        rendered but its credentials could not be resolved (e.g. an expired
        Databricks OAuth token). Pi still launches — its ``!command`` apiKey may
        recover at request time — but the caller surfaces this so a session that
        would otherwise fail silently tells the user how to re-authenticate.
    :param databricks_surfaces: Base URLs of the Databricks gateway surfaces
        reachable with this provider's credential, keyed by surface. Set by the
        Databricks builders; lets a model the live catalog didn't list be routed
        by family instead of stranded on the Claude-only primary.
    """

    provider_id: str
    base_url: str
    api: str
    model: str
    api_key: str
    auth_header: bool
    credential_warning: str | None = None
    # Full model list for providers that expose multiple models (e.g. the
    # Databricks Anthropic gateway). Excluded from __hash__ so the frozen
    # dataclass stays hashable even though list[dict] is not hashable.
    extra_models: list[_PiModelEntry] = field(default_factory=list, hash=False)
    # Extra providers to merge into models.json alongside the primary one (e.g.
    # an OpenAI Completions provider for GPT models on the Databricks gateway).
    # Keys are provider ids; values are complete Pi provider config dicts.
    additional_providers: dict[str, _PiProviderPayload] = field(default_factory=dict, hash=False)
    databricks_surfaces: dict[DatabricksPiSurface, str] = field(default_factory=dict, hash=False)

    @property
    def _primary_claude_only(self) -> bool:
        """Whether the primary provider can only serve Claude models.

        True for the Databricks gateway's ``/ai-gateway/anthropic`` surface.
        Deliberately not inferred from ``api == "anthropic-messages"``: a
        LiteLLM-style proxy speaks that protocol for arbitrary models, and
        inferring would strand those.
        """
        return bool(self.databricks_surfaces)

    def _model_registered_in_additional(self) -> bool:
        """Whether some secondary provider already serves the selected model."""
        return any(
            any(entry.get("id") == self.model for entry in provider["models"])
            for provider in self.additional_providers.values()
        )

    def _fallback_surface(self) -> DatabricksPiSurface | None:
        """Classify the selected model's surface when the catalog didn't list it.

        Returns ``None`` when no fallback applies — a non-Databricks primary
        (which picks ``api`` from the model's own family, so any id fits), a
        model Pi cannot parse at all, or a surface this credential can't reach.
        """
        if not self._primary_claude_only or unsupported_in_pi(self.model.lower()):
            return None
        surface = databricks_pi_surface_for_model(self.model)
        if surface is DatabricksPiSurface.ANTHROPIC:
            return surface
        return surface if surface in self.databricks_surfaces else None

    def unroutable_model_warning(self) -> str | None:
        """User-facing notice when no surface can serve the selected model.

        Pi launches with the model unregistered and fails on an unknown model,
        which reads to the user as another silent hang — so the caller surfaces
        this instead.

        :returns: The warning text, or ``None`` when the model is routable.
        """
        if self._model_registered_in_additional():
            return None
        if any(entry.get("id") == self.model for entry in self.extra_models):
            return None
        if self._fallback_surface() is not None:
            return None
        if not self._primary_claude_only:
            return None
        return (
            f"The model '{self.model}' can't be served by any endpoint this Pi session "
            "can reach, so it won't reply. The workspace model list was unavailable "
            "(expired credentials or an unreachable workspace) or doesn't include this "
            "endpoint. Pick a different model with `/model`, or re-authenticate and "
            "start a new Pi session."
        )

    def to_models_config(self) -> _PiModelsConfig:
        """Render this provider as a Pi ``models.json`` mapping."""
        models: list[_PiModelEntry] = list(self.extra_models)
        additional: dict[str, _PiProviderPayload] = dict(self.additional_providers)
        # Register the selected model only when no provider already serves it.
        # Appending a non-Claude model to the primary (Anthropic) provider would
        # register it under the wrong wire protocol — the gateway then rejects
        # the API type and the turn hangs with no reply.
        needs_registration = not self._model_registered_in_additional() and not any(
            entry.get("id") == self.model for entry in models
        )
        if needs_registration:
            surface = self._fallback_surface()
            if not self._primary_claude_only:
                # The primary's api came from the model's own family.
                models.append(
                    {"id": self.model, "input": ["text", "image"]}
                    if self.extra_models
                    else {"id": self.model}
                )
            elif surface is DatabricksPiSurface.ANTHROPIC:
                models.append({"id": self.model, "input": ["text", "image"]})
            elif surface is not None:
                self._register_on_surface(additional, surface)
            else:
                # Leave it unregistered so Pi fails fast on an unknown model
                # rather than hanging on a rejected API type. The caller
                # surfaces unroutable_model_warning() to explain it.
                _LOGGER.error(
                    "pi-native: no reachable Databricks surface can serve %r; leaving it "
                    "unregistered. The workspace model catalog was unavailable or omits "
                    "this endpoint.",
                    self.model,
                )
        provider: _PiProviderPayload = {
            "baseUrl": self.base_url,
            "api": self.api,
            "apiKey": self.api_key,
            "models": models,
        }
        if self.auth_header:
            provider["authHeader"] = True
        providers = {self.provider_id: provider}
        providers.update(additional)
        return {"providers": providers}

    def _register_on_surface(
        self, additional: dict[str, _PiProviderPayload], surface: DatabricksPiSurface
    ) -> None:
        """Add the selected model to *additional* under *surface*'s provider."""
        provider_id = _SURFACE_PROVIDER_IDS[surface]
        entry: _PiModelEntry = {"id": self.model, "input": ["text", "image"]}
        # DeepSeek streams on reasoning_content; Pi only reads that channel when
        # the model entry declares reasoning.
        if "deepseek" in self.model.lower():
            entry["reasoning"] = True
        existing = additional.get(provider_id)
        if existing is not None:
            # Copy rather than mutate: the payload is shared with
            # ``additional_providers``, and this renders more than once.
            additional[provider_id] = {**existing, "models": [*existing["models"], entry]}
            return
        responses = surface is DatabricksPiSurface.RESPONSES
        api_type = "openai-responses" if responses else "openai-completions"
        additional[provider_id] = _databricks_openai_provider(
            self.api_key, self.databricks_surfaces[surface], [entry], api_type=api_type
        )
        _LOGGER.info(
            "pi-native: %r was not in the workspace model catalog; routing it to the %s "
            "surface by model family.",
            self.model,
            surface.value,
        )


# DATABRICKS-PATCH(pi-live-model-discovery)
def _default_claude_model_from(entries: list[_PiModelEntry]) -> str | None:
    """Pick pi's launch model from the workspace's live Claude entries.

    ``_fetch_pi_model_lists`` reads Unity Catalog model services, so the servable
    ``system.ai.*`` ids are in hand; without this the launch fell back to the
    bundled MLflow catalog, whose legacy ``databricks-`` ids the gateway now
    answers with ``501 … Use Unity Catalog model services (v3)``.

    :param entries: Live Claude entries, e.g. ``[{"id": "system.ai.claude-opus-5"}]``.
    :returns: The best servable id, or ``None`` when the listing was empty.
    """
    from omnigent.databricks_model_discovery import _natural_model_key

    ids = [str(e["id"]) for e in entries if e.get("id")]
    # The precedence claude-native falls back to; newest within a tier.
    for tier in ("opus", "sonnet", "haiku", "fable"):
        matches = [i for i in ids if tier in i.lower()]
        if matches:
            return max(matches, key=_natural_model_key)
    return ids[0] if ids else None


def _databricks_pi_provider(entry: ProviderEntry, *, model: str | None) -> PiProviderConfig | None:
    """Resolve a Databricks-profile provider into Pi gateway config.

    :param entry: The resolved default provider entry (``kind="databricks"``).
    :param model: Session model override, or ``None`` to use the default.
    :returns: The Pi provider config, or ``None`` when the profile's host
        can't be resolved (caller falls back to Pi's own login).
    """
    # Imported lazily: codex_executor pulls in heavy inner deps, and this
    # module is imported on the runner's session-create path.
    from omnigent.inner.codex_executor import _databricks_codex_auth_command
    from omnigent.inner.databricks_executor import _read_databrickscfg_host

    host = _read_databrickscfg_host(entry.profile)
    if not host:
        return None
    host = host.rstrip("/")
    auth_command = _databricks_codex_auth_command(host, entry.profile)
    api_key = f"!{auth_command}"
    # Fetch the live model list from the workspace API so Pi's /model shows
    # exactly the endpoints available on this workspace. Falls back to the
    # bundled static lists when credentials can't be resolved or the API call
    # fails (e.g. network blip, new workspace with no endpoints yet).
    #
    # Distinguish two failure modes so the caller can surface the fatal one:
    #   * credential resolution fails (expired OAuth token) — Pi's per-request
    #     ``!command`` apiKey will also fail, so the session dies silently. Carry
    #     a ``credential_warning`` so the caller can tell the user to re-auth.
    #   * model-list fetch fails after creds resolved (network blip, empty
    #     workspace) — benign; just show the single default model.
    credential_warning: str | None = None
    claude_models: list[_PiModelEntry] = []
    gpt_models: list[_PiModelEntry] = []
    completions_models: list[_PiModelEntry] = []
    gemini_models: list[_PiModelEntry] = []
    try:
        creds = resolve_databricks_workspace(entry.profile)
    except Exception:  # noqa: BLE001 — credential failure must not break launch
        _LOGGER.info(
            "pi-native: falling back to single-model display (could not resolve credentials)"
        )
        credential_warning = _databricks_credential_warning(entry.profile)
    else:
        try:
            claude_models, gpt_models, completions_models, gemini_models = _fetch_pi_model_lists(
                creds.host, creds.token
            )
        except Exception:  # noqa: BLE001 — network failure must not break launch
            _LOGGER.info(
                "pi-native: could not fetch workspace model list; showing default model only"
            )
    additional: dict[str, _PiProviderPayload] = {}
    if gpt_models:
        additional[_PI_OPENAI_PROVIDER_ID] = _databricks_openai_provider(
            api_key, f"{host}/ai-gateway/codex/v1", gpt_models
        )
    if completions_models:
        additional[_PI_COMPLETIONS_PROVIDER_ID] = _databricks_openai_provider(
            api_key, f"{host}/serving-endpoints", completions_models, api_type="openai-completions"
        )
    if gemini_models:
        additional[_PI_MLFLOW_PROVIDER_ID] = _databricks_openai_provider(
            api_key, f"{host}/ai-gateway/mlflow/v1", gemini_models, api_type="openai-completions"
        )
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=f"{host}{_DATABRICKS_ANTHROPIC_GATEWAY_PATH}",
        api="anthropic-messages",
        # DATABRICKS-PATCH(pi-live-model-discovery): prefer what the workspace
        # actually serves (fetched above) over the bundled catalog.
        model=model
        or _default_claude_model_from(claude_models)
        or model_catalog.resolve_catalog_model("databricks", family="claude").model_id,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token is re-read per request (the auth command attempts a
        # refresh), matching codex-native's refresh semantics.
        api_key=api_key,
        auth_header=True,
        extra_models=claude_models,
        additional_providers=additional,
        credential_warning=credential_warning,
        databricks_surfaces={
            DatabricksPiSurface.RESPONSES: f"{host}/ai-gateway/codex/v1",
            DatabricksPiSurface.COMPLETIONS: f"{host}/serving-endpoints",
            DatabricksPiSurface.MLFLOW: f"{host}/ai-gateway/mlflow/v1",
        },
    )


def _databricks_credential_warning(profile: str | None) -> str:
    """User-facing notice for an unresolvable Databricks profile.

    :param profile: The Databricks config profile that failed to authenticate.
    :returns: A short message naming the profile and the re-auth command.
    """
    profile_name = profile or "DEFAULT"
    return (
        f"Couldn't authenticate to the Databricks profile '{profile_name}' — "
        "your login has likely expired, so this Pi session can't reach the model "
        "and won't reply. Re-authenticate by running "
        f"`databricks auth login --profile {profile_name}`, then start a new Pi session."
    )


def _databricks_openai_provider(
    api_key: str,
    base_url: str,
    models: list[_PiModelEntry],
    api_type: str = "openai-responses",
) -> _PiProviderPayload:
    """Build a Pi OpenAI provider config for Databricks models.

    ``api_type`` selects the wire protocol:

    * ``"openai-responses"`` — AI Gateway codex surface
      (``/ai-gateway/codex/v1``). Required for newer GPT models (gpt-5.5,
      gpt-5.6-*) that reject function tool calls via ``/chat/completions``.
    * ``"openai-completions"`` — workspace serving-endpoints surface. Works
      for Kimi, Llama, GLM, Gemini, and older GPT models.

    ``authHeader`` sends ``Authorization: Bearer {token}`` (Databricks requires
    this; without it the OpenAI SDK uses ``api-key`` which is rejected).
    """
    return {
        "baseUrl": base_url,
        "apiKey": api_key,
        "api": api_type,
        "authHeader": True,
        "compat": {
            "supportsDeveloperRole": False,
            "supportsStore": False,
            "supportsStrictMode": False,
            "supportsReasoningEffort": False,
            # stream_options is OpenAI-specific; Gemini and other non-OpenAI
            # models reject it with 400.
            "supportsUsageInStreaming": False,
        },
        "models": models,
    }


def _run_auth_command(auth_command: str, *, timeout: float = 15.0) -> str | None:
    """Run *auth_command* and return its stdout as a bearer token.

    Used to obtain a short-lived token at session-create time for the
    one-shot model-catalog API call. Returns ``None`` on any failure so
    callers can fall back gracefully.

    :param auth_command: Shell command string, e.g.
        ``"jq -r .access_token /path/token.json"``.
    :param timeout: Maximum seconds to wait for the command.
    :returns: Stripped stdout (the token), or ``None`` when the command
        fails, times out, or produces empty output.
    """
    try:
        result = subprocess.run(
            shlex.split(auth_command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:  # noqa: BLE001 — any subprocess failure should just return None
        return None


def _fetch_pi_model_lists(
    workspace_url: str,
    token: str,
) -> _PiModelLists:
    """Fetch live model lists from the Unity Catalog model-services API.

    Calls ``GET <workspace>/api/2.1/unity-catalog/model-services``, which
    returns ``system.ai.*`` model ids with their supported API types:

    * ``openai/v1/responses`` in supported_api_types → ``openai-responses``
      provider at the AI Gateway codex surface.
    * Chat-capable models without Responses API support → ``openai-completions``
      provider at the serving-endpoints surface.
    * Claude models → ``anthropic-messages`` provider.

    Using this API avoids the ``databricks-*`` → ``system.ai.*`` translation
    and gives authoritative API capability information per model.

    Falls back to empty lists on any HTTP or auth failure so a network blip
    never breaks Pi session launch.

    :param workspace_url: Databricks workspace base URL, e.g.
        ``"https://wkspc.example.com"`` — **no** trailing slash or path.
    :param token: Bearer token for the workspace API.
    :returns: ``(claude_models, gpt_responses_models, completions_models, gemini_models)`` —
        Pi model entry dicts ready to write into ``models.json``.
    """
    try:
        models = model_catalog.fetch_databricks_model_service_entries(workspace_url, token)
    except Exception:  # noqa: BLE001 — HTTP/network failure → empty
        _LOGGER.warning(
            "pi-native: could not fetch Databricks model list; "
            "Pi will show only the selected model",
            exc_info=True,
        )
        return [], [], [], []

    claude: list[_PiModelEntry] = []
    gpt_responses: list[_PiModelEntry] = []
    completions: list[_PiModelEntry] = []
    gemini: list[_PiModelEntry] = []

    # The model-service listing reports availability but no token limits; the
    # MLflow catalog reports limits but not what this workspace serves. Merge
    # them, or Pi falls back to its 128000/16384 defaults and silently truncates
    # the 1M-context gateway models. Best-effort: a catalog outage just means
    # the limits are omitted, exactly as before.
    try:
        models = enrich_databricks_model_catalog(
            models, model_catalog.catalog_model_entries("databricks")
        )
    except Exception:  # noqa: BLE001 — live availability remains authoritative
        _LOGGER.info(
            "pi-native: could not enrich the live Databricks model list with MLflow metadata",
            exc_info=True,
        )

    for model in models:
        name = model.id
        name_lower = name.lower()
        entry: _PiModelEntry = pi_model_json_entry(model)
        needs_responses = ModelWireAPI.OPENAI_RESPONSES in model.metadata.wire_apis or any(
            keyword in name_lower for keyword in SYSTEM_AI_RESPONSES_KEYWORDS
        )
        if "claude" in name_lower:
            claude.append(entry)
        elif unsupported_in_pi(name_lower):
            pass  # exclude (e.g. gemini-2-5 thinking models)
        elif needs_responses:
            # Responses API: GPT models that need it + kimi/inkling/qwen3/glm keywords.
            gpt_responses.append(entry)
        elif name_lower.startswith("system.ai."):
            # Other system.ai.* ids (Gemini, Llama) → mlflow gateway;
            # system.ai.* ids are not valid at /serving-endpoints.
            gemini.append(entry)
        else:
            completions.append(entry)

    if not claude and not gpt_responses and not completions and not gemini:
        _LOGGER.info(
            "pi-native: Unity Catalog model-services returned no LLM models; "
            "Pi will show only the selected model"
        )

    return claude, gpt_responses, completions, gemini


def _gateway_anthropic_base_url(codex_base_url: str) -> str:
    """Rewrite a Codex gateway base URL to the Anthropic Messages surface.

    The Databricks AI Gateway serves each protocol under the same workspace
    origin: ``.../codex/v1`` (OpenAI Responses) and ``.../anthropic``
    (Anthropic Messages). ``isaac configure codex`` records the Codex URL;
    Pi speaks Anthropic Messages natively, so we point it at ``/anthropic``.

    :param codex_base_url: The provider table's ``base_url``, e.g.
        ``"https://<workspace>.ai-gateway.cloud.databricks.com/codex/v1"``.
    :returns: The Anthropic-surface base URL, e.g.
        ``"https://<workspace>.ai-gateway.cloud.databricks.com/anthropic"``.
    """
    trimmed = codex_base_url.rstrip("/")
    if trimmed.endswith(_DATABRICKS_GATEWAY_CODEX_SUFFIX):
        trimmed = trimmed[: -len(_DATABRICKS_GATEWAY_CODEX_SUFFIX)]
    if trimmed.endswith(_DATABRICKS_GATEWAY_ANTHROPIC_SUFFIX):
        return trimmed
    return f"{trimmed}{_DATABRICKS_GATEWAY_ANTHROPIC_SUFFIX}"


def _cli_config_databricks_transport(entry: ProviderEntry) -> CodexConfigTransport | None:
    """Return the codex transport for a pi-consumable Databricks cli-config entry.

    Shared core of :func:`_cli_config_pi_provider` and
    :func:`cli_config_pi_provider_capable`: validates that *entry* is a codex
    ``cli-config`` whose pinned ``[model_providers.X]`` table in
    ``~/.codex/config.toml`` is a genuine Databricks AI Gateway carrying a
    bearer-token command. Returns the resolved
    :class:`~omnigent.onboarding.ambient.CodexConfigTransport` when so, else
    ``None`` (logging the reason at INFO).

    :param entry: The provider entry (expected ``kind="cli-config"``).
    :returns: The codex transport when *entry* is a pi-consumable Databricks
        AI Gateway, else ``None``.
    """
    # Only codex cli-config providers are model_provider-shaped today; a
    # claude analog would be a different mechanism entirely.
    if entry.cli != "codex" or not entry.model_provider:
        return None
    # Imported lazily: ambient pulls in onboarding-only deps, and this module
    # is imported on the runner's session-create hot path.
    from omnigent.onboarding.ambient import (
        _codex_config_path,
        codex_config_provider_transport,
    )

    transport = codex_config_provider_transport(_codex_config_path(), entry.model_provider)
    if transport is None:
        # The model_provider may live in a sibling config file (e.g. config1.toml
        # used by ucode / Codex app profile switching). Scan other config*.toml
        # files in ~/.codex/ for the matching model_provider table.
        codex_dir = _codex_config_path().parent
        for alt_config in sorted(codex_dir.glob("config*.toml")):
            if alt_config == _codex_config_path():
                continue
            transport = codex_config_provider_transport(alt_config, entry.model_provider)
            if transport is not None:
                _LOGGER.info(
                    "pi-native: cli-config provider %r (model_provider %r) found in %s",
                    entry.name,
                    entry.model_provider,
                    alt_config.name,
                )
                break
    if transport is None:
        _LOGGER.info(
            "pi-native: cli-config provider %r (model_provider %r) has no resolvable "
            "[model_providers.%s] base_url in ~/.codex/config*.toml; Pi will use its own login.",
            entry.name,
            entry.model_provider,
            entry.model_provider,
        )
        return None
    # Identify the Databricks AI Gateway robustly (not by workspace id): parse
    # the codex base_url and validate its *hostname* against a trusted
    # Databricks domain suffix allowlist plus the ``ai-gateway`` DNS label — a
    # substring scan over the whole base_url would forward the workspace bearer
    # token to look-alike hosts (e.g. ``databricks-ai-gateway.evil.test``).
    if not _is_databricks_ai_gateway_url(transport.base_url):
        _LOGGER.info(
            "pi-native: cli-config provider %r (model_provider %r, base_url %r) is not a "
            "recognized Databricks AI Gateway; Pi will use its own login.",
            entry.name,
            entry.model_provider,
            transport.base_url,
        )
        return None
    if not transport.auth_command:
        # No explicit auth command (e.g. ucode config using ambient SDK auth).
        # Try to build a !command using the SDK, same as the databricks-kind path.
        try:
            from omnigent.inner.codex_executor import _databricks_codex_auth_command

            ws = resolve_databricks_workspace(None)
            auth_cmd = _databricks_codex_auth_command(ws.host, None)
            transport = CodexConfigTransport(
                base_url=transport.base_url,
                auth_command=auth_cmd,
            )
            _LOGGER.info(
                "pi-native: cli-config provider %r has no auth command; "
                "using SDK-derived auth for %s",
                entry.name,
                ws.host,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.info(
                "pi-native: Databricks cli-config provider %r (model_provider %r) "
                "has no auth command and SDK auth is unavailable; Pi will use its own login.",
                entry.name,
                entry.model_provider,
            )
            return None
    return transport


def cli_config_pi_provider_capable(entry: ProviderEntry) -> bool:
    """Return whether a ``cli-config`` *entry* is pi-consumable.

    A codex ``cli-config`` provider IS reusable by Pi exactly when
    :func:`_cli_config_pi_provider` would resolve — i.e. its pinned
    ``[model_providers.X]`` table is a genuine Databricks AI Gateway with a
    bearer-token command. This is the capability predicate the selection layer
    (:mod:`omnigent.onboarding.provider_config`) consults to decide whether a
    cli-config provider may serve / default the ``pi`` surface, keeping the
    single source of truth here (and avoiding an import cycle —
    ``provider_config`` lazy-imports this rather than the reverse).

    :param entry: The provider entry to classify (expected
        ``kind="cli-config"``; any other kind returns ``False``).
    :returns: ``True`` iff Pi can route through this cli-config provider.
    """
    return _cli_config_databricks_transport(entry) is not None


def _cli_config_pi_provider(entry: ProviderEntry, *, model: str | None) -> PiProviderConfig | None:
    """Resolve a Codex ``cli-config`` Databricks-gateway provider into Pi config.

    The common enterprise setup: ``isaac configure codex`` writes a custom
    ``[model_providers.X]`` table (base_url + token-printing ``auth`` command)
    into ``~/.codex/config.toml`` and ``omnigent setup`` adopts it as a
    ``cli-config`` provider. Codex-native routes through that table; pi-native
    used to return ``None`` here — silently falling back to Pi's own
    ``/login`` (often stale creds) — which is the bug this fixes.

    We read the *transport* (base URL + bearer-token command) from the codex
    config table the entry pins, rewrite the base URL to the gateway's
    Anthropic Messages surface (Pi speaks it natively), and emit a ``!command``
    apiKey so Pi refreshes the gateway token per request — exactly like the
    ``databricks`` kind path. The workspace-specific base URL and token path
    are read from config, never hardcoded.

    :param entry: The resolved default provider (``kind="cli-config"``,
        ``cli="codex"``), carrying the ``model_provider`` id and display name.
    :param model: Session model override, or ``None`` to use the default.
    :returns: The Pi provider config, or ``None`` when the entry is not a
        Databricks gateway, its codex provider table can't be resolved, or it
        carries no token command (caller falls back to Pi's own login).
    """
    transport = _cli_config_databricks_transport(entry)
    if transport is None:
        return None
    api_key = f"!{transport.auth_command}"
    # The AI Gateway hostname (e.g. ``<id>.ai-gateway.cloud.databricks.com``)
    # is NOT the workspace hostname — stripping ``ai-gateway.`` produces an
    # NXDOMAIN. Use resolve_databricks_workspace for the real workspace URL,
    # but use the auth_command token (same credential the gateway uses) for
    # the API call. The SDK's minted token may not have serving-endpoints
    # access on workspaces where access is controlled via the auth command.
    claude_models: list[_PiModelEntry] = []
    gpt_models: list[_PiModelEntry] = []
    completions_models: list[_PiModelEntry] = []
    gemini_models: list[_PiModelEntry] = []
    parsed_gateway = urlparse(transport.base_url)
    gateway_labels = (parsed_gateway.hostname or "").split(".")
    real_workspace_url = _databricks_workspace_url_for_gateway(transport.base_url)
    if real_workspace_url is None:
        _LOGGER.info(
            "pi-native: cli-config path could not resolve workspace URL "
            "for model listing; Pi will show only the selected model"
        )
    if real_workspace_url and transport.auth_command:
        token = _run_auth_command(transport.auth_command)
        if token:
            claude_models, gpt_models, completions_models, gemini_models = _fetch_pi_model_lists(
                real_workspace_url, token
            )
        else:
            _LOGGER.info(
                "pi-native: auth command produced no token; Pi will show only the selected model"
            )
    # Derive the AI Gateway codex URL for the openai-responses provider. For
    # workspace-hosted URLs the transport base is already the codex path;
    # for dedicated-subdomain URLs we build it from the workspace URL.
    if _DATABRICKS_AI_GATEWAY_LABEL in gateway_labels:
        # Dedicated subdomain: transport.base_url is the codex gateway URL.
        # Strip trailing path suffixes to get the codex base, not /anthropic.
        codex_gateway_url = transport.base_url.rstrip("/")
        if codex_gateway_url.endswith(_DATABRICKS_GATEWAY_CODEX_SUFFIX):
            codex_gateway_url = codex_gateway_url[: -len(_DATABRICKS_GATEWAY_CODEX_SUFFIX)]
        codex_gateway_url = f"{codex_gateway_url}{_DATABRICKS_GATEWAY_CODEX_SUFFIX}"
    else:
        # Workspace-hosted gateway: build from workspace hostname.
        codex_gateway_url = f"https://{parsed_gateway.hostname}/ai-gateway/codex/v1"
    workspace_completions_url = (
        real_workspace_url + "/serving-endpoints" if real_workspace_url else None
    )
    workspace_mlflow_url = (
        real_workspace_url + "/ai-gateway/mlflow/v1" if real_workspace_url else None
    )
    additional: dict[str, _PiProviderPayload] = {}
    if gpt_models:
        additional[_PI_OPENAI_PROVIDER_ID] = _databricks_openai_provider(
            api_key, codex_gateway_url, gpt_models
        )
    if completions_models and workspace_completions_url:
        additional[_PI_COMPLETIONS_PROVIDER_ID] = _databricks_openai_provider(
            api_key, workspace_completions_url, completions_models, api_type="openai-completions"
        )
    if gemini_models and workspace_mlflow_url:
        additional[_PI_MLFLOW_PROVIDER_ID] = _databricks_openai_provider(
            api_key, workspace_mlflow_url, gemini_models, api_type="openai-completions"
        )
    surfaces = {DatabricksPiSurface.RESPONSES: codex_gateway_url}
    if workspace_completions_url:
        surfaces[DatabricksPiSurface.COMPLETIONS] = workspace_completions_url
    if workspace_mlflow_url:
        surfaces[DatabricksPiSurface.MLFLOW] = workspace_mlflow_url
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=_gateway_anthropic_base_url(transport.base_url),
        api="anthropic-messages",
        model=model or model_catalog.resolve_catalog_model("databricks", family="claude").model_id,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token (the codex auth command prints it) is refreshed per
        # request — matching codex-native's refresh semantics.
        api_key=api_key,
        auth_header=True,
        extra_models=claude_models,
        additional_providers=additional,
        databricks_surfaces=surfaces,
    )


def _inline_family_order(model: str | None) -> tuple[str, ...]:
    """Order the inline families to try, model's own family first.

    Only Claude ids prefer the Anthropic family. Everything else — GPT, and the
    Gemini/Llama/DeepSeek ids that token as ``"other"`` — is served over an
    OpenAI-compatible wire by nearly every gateway, so it leads with OpenAI.
    With no model to go on, Anthropic leads: Pi speaks it natively.
    """
    # An Anthropic-wire-only non-Claude id would prefer the wrong surface here;
    # only a dual-surface provider is exposed, since the loop falls through.
    if model and model_catalog.model_family_token(model) != "claude":
        return ("openai", "anthropic")
    return ("anthropic", "openai")


def _inline_family_pi_provider(
    entry: ProviderEntry, *, model: str | None
) -> PiProviderConfig | None:
    """Resolve a key/gateway/local provider into Pi config from its family.

    Tries the family matching the selected model first, so a provider offering
    both surfaces serves a GPT id from its OpenAI family rather than whichever
    family happens to be configured first. Falls back to the other family, which
    keeps protocol-translating proxies working: a LiteLLM ``/anthropic``
    passthrough is the only configured family and still serves any model.

    :param entry: The resolved default provider entry.
    :param model: Session model override, or ``None`` to use the family default.
    :returns: The Pi provider config, or ``None`` when no usable family with a
        base URL and credential is configured.
    """
    for family_name in _inline_family_order(model):
        family = entry.family(family_name)
        if family is None or not family.base_url:
            continue
        # Determine the API type based on family and wire_api setting.
        if family_name == "anthropic":
            api = "anthropic-messages"
        elif family.wire_api == CHAT_WIRE_API:
            api = "openai-completions"
        else:
            api = "openai-responses"
        # A static key (or $VAR) — Pi reads a literal/env apiKey directly; an
        # auth_command becomes a "!command" Pi resolves at request time.
        if family.api_key:
            api_key = family.api_key
            auth_header = False
        elif family.auth_command:
            api_key = f"!{family.auth_command}"
            auth_header = True
        else:
            continue
        resolved_model = model or entry.family_default_model(family_name)
        if not resolved_model:
            continue
        # A session model override can arrive as a Databricks-gateway id
        # (``databricks-claude-opus-4-7``) — that prefix only routes through the
        # Databricks AI Gateway (``_databricks_pi_provider``). This family is
        # vendor-direct (key / inline gateway / local Anthropic|OpenAI endpoint),
        # so strip the mechanical ``databricks-`` prefix to the bare vendor id
        # the endpoint can actually route. ``normalize_model_for_provider`` is
        # prefix-mechanical: it only strips ``databricks-claude-*``/
        # ``databricks-gpt-*`` and passes non-mechanical ids (e.g.
        # ``zai-org/GLM-4.7``) and already-bare ids through unchanged. Family
        # defaults are bare, so the no-override path is unaffected.
        resolved_model = normalize_model_for_provider(resolved_model, KEY_KIND)
        # Strip bracket suffixes (e.g. "[1m]") — accepted by the direct
        # Anthropic API but rejected by the Databricks AI Gateway.
        resolved_model = re.sub(r"\[.*?\]$", "", resolved_model)
        return PiProviderConfig(
            provider_id=_PI_PROVIDER_ID,
            base_url=family.base_url,
            api=api,
            model=resolved_model,
            api_key=api_key,
            auth_header=auth_header,
        )
    return None


def resolve_pi_native_provider(
    *,
    model: str | None = None,
    config_loader: Callable[[], dict[str, object]] = load_config,
) -> PiProviderConfig | None:
    """Resolve the omnigent-configured provider for a native Pi session.

    Reads the default provider for the Pi surface from
    ``~/.omnigent/config.yaml`` and translates it into Pi ``models.json``
    config. Returns ``None`` — leaving Pi to use its own ``/login`` — when no
    usable provider is configured, or the default is a subscription / CLI-login
    provider (a CLI's own login can't be reused outside that CLI).

    :param model: Session model override (``model_override``), or ``None`` to
        use the provider's default model.
    :param config_loader: Injection seam for tests; defaults to
        :func:`load_config`.
    :returns: The resolved provider config, or ``None`` to fall back to Pi's
        own credentials.
    """
    try:
        config = config_loader()
        # Pi is multi-family; ``omnigent setup`` marks defaults per family, not
        # for ``pi``. Use the shared house-pattern selection so pi resolves its
        # default exactly like the rest of the codebase — an explicit pi default
        # wins, else the anthropic (Pi's native surface) then openai family
        # default, skipping kinds that can't drive pi. Crucially this now lets a
        # cli-config Databricks AI Gateway through (it is pi-consumable via
        # ``_cli_config_pi_provider``), so an unrelated anthropic-family default
        # no longer shadows it.
        entry = default_provider_for_harness(config, PI_SURFACE)
        if entry is None:
            _LOGGER.info(
                "pi-native: no omnigent-configured provider for the pi/anthropic/openai "
                "surface; Pi will use its own login."
            )
            return None
        if entry.kind == DATABRICKS_KIND:
            resolved = _databricks_pi_provider(entry, model=model)
        elif entry.kind == CLI_CONFIG_KIND:
            # A Codex cli-config provider whose [model_providers.X] table is the
            # Databricks AI Gateway IS reusable by Pi (the gateway exposes an
            # Anthropic surface Pi speaks). Translate it rather than dropping to
            # Pi's own login — the bug this module fixes.
            resolved = _cli_config_pi_provider(entry, model=model)
        elif entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
            resolved = _inline_family_pi_provider(entry, model=model)
        else:
            # subscription (a CLI's own login can't be reused outside that CLI):
            # let Pi use its own login.
            _LOGGER.info(
                "pi-native: configured provider %r (kind %r) cannot drive Pi; "
                "Pi will use its own login.",
                entry.name,
                entry.kind,
            )
            return None
        if resolved is None:
            # The provider matched a translatable kind but its details could not
            # be resolved (e.g. a Databricks gateway whose codex config table is
            # missing). Try the databricks-kind provider as a fallback — a common
            # setup has a cli-config pi default alongside a databricks-kind
            # provider that carries the actual workspace credentials.
            _LOGGER.warning(
                "pi-native: configured provider %r (kind %r) could not be translated "
                "into native Pi config; trying databricks-kind fallback.",
                entry.name,
                entry.kind,
            )
            from omnigent.onboarding.provider_config import _parse_provider

            providers = config.get("providers")
            db_entry = next(
                (
                    _parse_provider(name, raw)
                    for name, raw in (providers.items() if isinstance(providers, dict) else ())
                    if isinstance(name, str)
                    and _is_str_object_dict(raw)
                    and raw.get("kind") == DATABRICKS_KIND
                ),
                None,
            )
            if db_entry is not None:
                resolved = _databricks_pi_provider(db_entry, model=model)
            if resolved is None:
                _LOGGER.warning("pi-native: no usable provider found; Pi will use its own login.")
        return resolved
    except Exception:  # noqa: BLE001 — any resolution failure must not break launch
        # Any failure (malformed config, duplicate per-family default, or an
        # unresolved ``api_key: $VAR``) falls back to Pi's own login rather than
        # failing the terminal launch.
        _LOGGER.warning(
            "pi-native: failed to resolve the omnigent-configured provider; Pi will "
            "use its own login.",
            exc_info=True,
        )
        return None


def write_pi_models_config(
    agent_dir: Path,
    provider: PiProviderConfig,
    rendered: _PiModelsConfig | None = None,
) -> Path:
    """Write *provider* as ``models.json`` into a managed Pi config dir.

    :param agent_dir: The managed Pi config dir (``PI_CODING_AGENT_DIR``).
    :param provider: The resolved provider config to render.
    :param rendered: An already-rendered config to write, so a caller that also
        inspects it renders (and logs) only once. Defaults to rendering here.
    :returns: Path to the written ``models.json``.
    """
    if rendered is None:
        rendered = provider.to_models_config()
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent_dir, 0o700)
    models_path = agent_dir / "models.json"
    # 0o600: the apiKey may be a literal token (key-kind providers).
    fd = os.open(models_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(rendered, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return models_path


def pi_native_provider_launch(
    agent_dir: Path, provider: PiProviderConfig
) -> tuple[dict[str, str], list[str]]:
    """Write the managed config and return the launch env + CLI args for Pi.

    :param agent_dir: The managed Pi config dir for this session.
    :param provider: The resolved provider config.
    :returns: ``(env, args)`` — the env vars to merge into the terminal spec
        (relocating Pi's config dir) and the ``--provider``/``--model`` args to
        append to the Pi command.
    """
    # Render once and reuse: rendering logs how an uncataloged model was routed,
    # and this function both writes the config and reads it back for --provider.
    rendered = provider.to_models_config()
    write_pi_models_config(agent_dir, provider, rendered)
    # Copy the user's global Pi settings but suppress defaultThinkingLevel.
    # In TUI mode Pi applies the setting from ~/.pi/agent/settings.json; for
    # non-Claude models via openai-completions, any thinking level causes the
    # Databricks gateway to return 400 (reasoning_effort is sent even when
    # supportsReasoningEffort is false in the compat block, because TUI mode
    # applies the session-level thinking before the compat check fires).
    # Passing None in the overlay makes _deep_merge_settings write null for the
    # key; Pi's getDefaultThinkingLevel() returns null (falsy) → no thinking.
    from omnigent.inner.pi_settings import prepare_managed_pi_agent_dir

    prepare_managed_pi_agent_dir(agent_dir, overlay={"defaultThinkingLevel": None})
    env = {PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir)}
    # Resolve which provider the selected model lives in. Non-Claude models
    # (GLM, GPT, Llama…) are in secondary providers (omnigent-openai); Claude
    # models are in the primary provider (omnigent). Read the *rendered* config
    # rather than additional_providers so a model routed by the family fallback
    # gets the same --provider that models.json registered it under.
    model_provider_id = provider.provider_id
    for extra_id, extra_cfg in rendered["providers"].items():
        if extra_id == provider.provider_id:
            continue
        if any(m.get("id") == provider.model for m in extra_cfg.get("models", [])):
            model_provider_id = extra_id
            break
    # When the model id contains a "/" Pi's arg parser splits on the first
    # slash and treats the left part as a provider name, overriding
    # --provider. Pass the fully-qualified "provider/model" reference so Pi's
    # findExactModelReferenceMatch matches the canonical form exactly and
    # routes to our custom provider, not a builtin with the same model id.
    model_arg = (
        f"{model_provider_id}/{provider.model}" if "/" in provider.model else provider.model
    )
    args = ["--provider", model_provider_id, "--model", model_arg]
    # For non-Claude models on openai-completions/responses, disable thinking.
    # Gemini and other Databricks models return reasoning_tokens in their
    # responses; Pi's TUI mode applies thinking even with defaultThinkingLevel:null
    # in settings, causing the agent loop to complete without surfacing the text
    # content to the extension. Explicitly passing --thinking off ensures the
    # completions handler doesn't activate the thinking path.
    if model_provider_id != provider.provider_id:
        args.extend(["--thinking", "off"])
    return env, args
