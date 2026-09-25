"""Synthesize OpenCode provider config for the native-server harness.

Unlike codex/claude/pi — which consume ``HARNESS_*_GATEWAY_*`` env vars that
their CLIs translate into provider config — OpenCode reads its provider/auth
from its own config file under the per-session ``XDG_CONFIG_HOME``. So routing
opencode-native through the Databricks AI gateway (or any OpenAI-compatible
endpoint) means writing an ``opencode.json`` into the runner-owned
``opencode serve``'s config dir at spawn, declaring a custom
``@ai-sdk/openai-compatible`` provider pointed at ``{host}/serving-endpoints``.

The model is then referenced as ``<provider_id>/<endpoint>`` per prompt.

Security: the file carries a bearer token, so it is written ``0600`` into the
per-session XDG dir (never the user's global ``~/.config/opencode``). The token
is resolved at spawn; a resumed session re-spawns the server and re-resolves, so
short-lived gateway tokens refresh on resume (documented limitation: a token
that expires mid-session is not refreshed in place).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.models import model_catalog

if TYPE_CHECKING:
    from databricks.sdk.core import Config

    from omnigent.onboarding.provider_config import FamilyConfig
    from omnigent.spec.types import MCPServerConfig

_logger = logging.getLogger(__name__)

# Provider id used in the synthesized opencode.json for the Databricks gateway.
# The per-prompt model is pinned as ``{DATABRICKS_GATEWAY_PROVIDER_ID}/<endpoint>``.
DATABRICKS_GATEWAY_PROVIDER_ID = "databricks-gateway"
DATABRICKS_GATEWAY_PROVIDER_NAME = "Databricks AI Gateway"
# Endpoint that exposes the workspace's OpenAI-compatible chat completions.
_SERVING_ENDPOINTS_PATH = "serving-endpoints"
# Optional deployment default: a ``databricks-*`` serving-endpoint id used when a
# session pins no compatible model. Unset falls back to the Databricks Claude
# catalog. Set it in the runner env to steer every session at one endpoint
# (e.g. ``databricks-kimi-k3``).
DATABRICKS_GATEWAY_DEFAULT_MODEL_ENV_VAR = "OMNIGENT_DATABRICKS_GATEWAY_MODEL"


@dataclass(frozen=True)
class OpenCodeGatewayResolution:
    """A resolved OpenAI-compatible gateway for the opencode-native harness.

    :param base_url: OpenAI-compatible base URL, e.g.
        ``"https://ws.cloud.databricks.com/serving-endpoints"``.
    :param api_key: Bearer token / API key for the gateway.
    :param model_id: The endpoint/model id, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param model_ids: Every serving-endpoint the gateway can route to, so opencode's
        in-session model picker lists them all. ``model_id`` stays the pinned launch
        default. Empty falls back to just ``model_id``.
    :param provider_id: opencode provider id, e.g. ``"databricks-gateway"``.
    :param provider_name: Human label for the opencode provider block.
    """

    base_url: str
    api_key: str
    model_id: str
    model_ids: tuple[str, ...] = ()
    provider_id: str = DATABRICKS_GATEWAY_PROVIDER_ID
    provider_name: str = DATABRICKS_GATEWAY_PROVIDER_NAME

    @property
    def qualified_model(self) -> str:
        """:returns: The per-prompt ``provider/model`` id opencode expects."""
        return f"{self.provider_id}/{self.model_id}"


def resolve_bound_opencode_gateway(
    *, model: str | None = None, auth: object = None
) -> OpenCodeGatewayResolution | None:
    """Resolve an explicit session binding without consulting Connect fallbacks."""
    from omnigent.inference_config import (
        binding_for_harness,
        load_runtime_inference_config,
        resolve_bound_model,
        resolve_bound_provider,
    )
    from omnigent.onboarding.provider_config import OPENAI_FAMILY

    config = load_runtime_inference_config()
    entry = resolve_bound_provider(config, "opencode-native", auth)
    if entry is None:
        return None
    family = entry.family(OPENAI_FAMILY)
    selected = resolve_bound_model(config, "opencode-native", model)
    if family is None or not selected:
        raise ValueError("OpenCode requires an OpenAI-compatible provider and a default model.")
    if family.wire_api == "responses":
        raise ValueError("OpenCode's configured gateway must support the chat wire API.")
    token = model_catalog._resolve_bearer_token(
        model_catalog.ResolvedModelProvider(
            kind=entry.kind, api_key=family.api_key, auth_command=family.auth_command
        )
    )
    binding = binding_for_harness(config, "opencode-native")
    return OpenCodeGatewayResolution(
        base_url=family.base_url,
        api_key=token,
        model_id=selected,
        model_ids=binding.model_allowlist or () if binding is not None else (),
        provider_id="omnigent",
        provider_name=entry.name,
    )


def build_opencode_model_default_config(model: str) -> dict[str, object]:
    """
    Build a minimal ``opencode.json`` that only pins the default model.

    Used when the user's own provider auth (``opencode auth login`` /
    provider env keys) already supplies credentials, but a default model has
    been chosen — via ``omni opencode --model`` or the ``omni setup`` OpenCode
    default — so the per-session TUI (and the first turn) launch on that model
    instead of OpenCode's built-in default (``opencode/big-pickle``). No
    provider block: OpenCode resolves the provider from the model id's prefix
    against its own ``auth.json``.

    :param model: A ``provider/model`` id, e.g. ``"anthropic/claude-sonnet-4-5"``.
    :returns: A config dict ready to serialize to ``opencode.json``.
    """
    return {"$schema": "https://opencode.ai/config.json", "model": model}


# Hide OpenCode's built-in free tier when Omnigent supplies a provider.
_OPENCODE_AUTOLOADED_FREE_PROVIDERS = ("opencode",)


def disable_autoloaded_free_providers(config: dict[str, object]) -> dict[str, object]:
    """Hide opencode's auto-loaded free providers (Zen / ``big-pickle``) from the picker.

    Only hides the free tier when Omnigent actually supplies a replacement — a
    synthesized ``provider`` block or a pinned non-free model. A config that
    carries only MCP/plugin wiring (no provider, no model) leaves the free tier
    usable, and an explicitly selected ``opencode/...`` model is preserved.
    """
    if not config:
        return config
    model = config.get("model")
    model_provider = model.split("/", 1)[0] if isinstance(model, str) and model else None
    if model_provider in _OPENCODE_AUTOLOADED_FREE_PROVIDERS:
        # The user pinned the free tier itself; keep it available.
        return config
    supplies_replacement = bool(config.get("provider")) or bool(model_provider)
    if not supplies_replacement:
        return config
    config.setdefault("$schema", "https://opencode.ai/config.json")
    config["disabled_providers"] = list(_OPENCODE_AUTOLOADED_FREE_PROVIDERS)
    return config


def build_opencode_provider_config(resolution: OpenCodeGatewayResolution) -> dict[str, object]:
    """
    Build the ``opencode.json`` declaring a custom OpenAI-compatible provider.

    :param resolution: The resolved gateway (base URL + key + model).
    :returns: A config dict ready to serialize to ``opencode.json``.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            resolution.provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": resolution.provider_name,
                "options": {
                    "baseURL": resolution.base_url,
                    "apiKey": resolution.api_key,
                },
                "models": {
                    mid: {"name": mid} for mid in (resolution.model_ids or (resolution.model_id,))
                },
            }
        },
    }


_AI_SDK_ANTHROPIC = "@ai-sdk/anthropic"
_AI_SDK_OPENAI_COMPATIBLE = "@ai-sdk/openai-compatible"
# Reasoning effort requires the Responses-capable OpenAI factory.
_AI_SDK_OPENAI = "@ai-sdk/openai"
# The SDK requires a key before the auth plugin can replace it.
_AUTH_PLUGIN_PLACEHOLDER_KEY = "omnigent-gateway-auth-plugin"
# Keep Responses models separate from chat-only aliases such as GLM.
_OPENAI_RESPONSES_GROUP = "openai-responses"


@dataclass(frozen=True)
class ConfigGatewayResolution:
    """A resolved ``config.yaml`` gateway provider for the opencode harness."""

    config: dict[str, object]
    auth_commands: dict[str, str]
    model: str


def _config_gateway_provider_id(entry_name: str, family: str) -> str:
    """Build a stable, JSON/id-safe opencode provider id for a family block."""
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", entry_name).strip("-") or "gateway"
    return f"{slug}-{family}"


def _strip_model_suffix(model_id: str) -> str:
    """Strip a trailing ``[...]`` suffix (e.g. ``[1m]``) from a model id."""
    return re.sub(r"\[.*?\]$", "", model_id)


def _append_unique_model(model_ids: list[str], model_id: str) -> None:
    """Append *model_id* (suffix-stripped) to *model_ids* if not already present."""
    stripped = _strip_model_suffix(model_id)
    if stripped and stripped not in model_ids:
        model_ids.append(stripped)


def _gateway_host_from_base_url(base_url: str) -> str | None:
    """Extract the bare workspace origin (``scheme://host``) from a family base URL."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url)
    except Exception:  # noqa: BLE001 - a malformed base URL just disables discovery.
        return None
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _derive_model_services_parent(families: list[tuple[str, str, FamilyConfig]]) -> str | None:
    """Derive the Unity Catalog parent (``schemas/<catalog>.<schema>``) from config."""
    for _family_name, _npm, family in families:
        for model_id in family.models.values():
            parts = _strip_model_suffix(model_id).split(".")
            if len(parts) >= 3 and parts[0] and parts[1]:
                return f"schemas/{parts[0]}.{parts[1]}"
    return None


def _mint_gateway_discovery_token(families: list[tuple[str, str, FamilyConfig]]) -> str | None:
    """Mint a bearer for the one-shot discovery API call from a family's auth."""
    for _family_name, _npm, family in families:
        if family.auth_command:
            try:
                completed = subprocess.run(
                    family.auth_command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
            except subprocess.TimeoutExpired:
                # Never log the exception/command: an inline credential in the
                # auth_command would leak into logs via the command text.
                _logger.info("opencode gateway discovery: auth command timed out.")
                continue
            except Exception:  # noqa: BLE001 - try the next family, else fall back to static.
                _logger.info("opencode gateway discovery: auth command failed.")
                continue
            token = completed.stdout.strip()
            if token:
                return token
        elif family.api_key:
            return family.api_key
    return None


def _gateway_model_family(model: model_catalog.ModelEntry) -> str | None:
    """Classify a discovered model-service into an opencode provider group."""
    from omnigent.models.model_metadata import ModelWireAPI
    from omnigent.models.pi_model_compatibility import (
        SYSTEM_AI_RESPONSES_KEYWORDS,
        unsupported_in_pi,
    )
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, OPENAI_FAMILY

    name_lower = model.id.lower()
    # Wire API is authoritative; name inference is a fallback for services
    # whose catalog metadata is incomplete (e.g. omni-sonnet-high has no
    # "claude" in the alias but its wire_apis carry ANTHROPIC_MESSAGES).
    if ModelWireAPI.ANTHROPIC_MESSAGES in model.metadata.wire_apis or "claude" in name_lower:
        return ANTHROPIC_FAMILY
    if unsupported_in_pi(name_lower):
        return None
    is_system_ai = name_lower.startswith("system.ai.")
    is_non_system_glm = "glm-" in name_lower and not is_system_ai
    needs_responses = not is_non_system_glm and (
        ModelWireAPI.OPENAI_RESPONSES in model.metadata.wire_apis
        or (
            is_system_ai and any(keyword in name_lower for keyword in SYSTEM_AI_RESPONSES_KEYWORDS)
        )
    )
    if needs_responses:
        return _OPENAI_RESPONSES_GROUP
    if is_system_ai:
        # OpenCode has no provider for the system.ai MLflow surface.
        return None
    return OPENAI_FAMILY


_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _clamp_effort(requested: str, allowed: tuple[str, ...]) -> str | None:
    """Clamp a requested canonical effort to a model's advertised ladder."""
    allowed_set = {effort for effort in allowed if effort in _EFFORT_ORDER}
    if not allowed_set:
        return None
    if requested in allowed_set:
        return requested
    req_idx = _EFFORT_ORDER.index(requested) if requested in _EFFORT_ORDER else len(_EFFORT_ORDER)
    at_or_below = [
        effort
        for effort in _EFFORT_ORDER
        if effort in allowed_set and _EFFORT_ORDER.index(effort) <= req_idx
    ]
    if at_or_below:
        return at_or_below[-1]
    return next(effort for effort in _EFFORT_ORDER if effort in allowed_set)


def _discover_gateway_models(
    families: list[tuple[str, str, FamilyConfig]],
) -> tuple[dict[str, list[str]], dict[str, tuple[str, ...]], set[str]]:
    """Live Unity Catalog model-service discovery, partitioned by workspace origin.

    Each family's catalog is fetched from its OWN origin (minting only same-origin
    credentials), so a model discovered on one workspace is never attributed to a
    provider on another.

    :returns: ``(groups, efforts, discovered_groups)`` — the discovered model ids
        per group, the reasoning efforts per Responses model, and the set of
        groups whose origin discovery succeeded. Those groups are authoritative;
        every other group keeps its configured tiers.
    """
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, OPENAI_FAMILY

    if not families:
        return {}, {}, set()

    # Discover each family from its OWN (origin, Unity-Catalog schema): two
    # families can share a workspace host but list under different parents, so a
    # single derived schema would attribute one family's catalog to the other.
    # Cache per (origin, parent) so families that do share both fetch once, and
    # mint only from same-origin families so no credential crosses hosts.
    buckets: dict[str, list[str]] = {
        ANTHROPIC_FAMILY: [],
        OPENAI_FAMILY: [],
        _OPENAI_RESPONSES_GROUP: [],
    }
    efforts: dict[str, tuple[str, ...]] = {}
    discovered_groups: set[str] = set()
    catalogs: dict[tuple[str, str], list[model_catalog.ModelEntry]] = {}
    for name, npm, family in families:
        host = _gateway_host_from_base_url(family.base_url)
        parent = _derive_model_services_parent([(name, npm, family)])
        if not host or not parent:
            continue
        key = (host, parent)
        if key not in catalogs:
            same_origin = [
                spec for spec in families if _gateway_host_from_base_url(spec[2].base_url) == host
            ]
            token = _mint_gateway_discovery_token(same_origin)
            if not token:
                continue
            try:
                entries = model_catalog.fetch_databricks_model_service_entries(
                    host, token, model_services_parent=parent, strict_details=True
                )
            except Exception:  # noqa: BLE001 - discovery failure falls back to static tiers.
                _logger.info(
                    "opencode gateway discovery: model-service listing failed for %s (%s); "
                    "using config tiers for its family.",
                    host,
                    parent,
                    exc_info=True,
                )
                continue
            catalogs[key] = list(entries)
        # This family's own schema was reached, so its groups are authoritative.
        allowed = (
            {ANTHROPIC_FAMILY}
            if name == ANTHROPIC_FAMILY
            else {OPENAI_FAMILY, _OPENAI_RESPONSES_GROUP}
        )
        discovered_groups |= allowed
        for model in catalogs[key]:
            group = _gateway_model_family(model)
            if group not in buckets or group not in allowed:
                continue
            _append_unique_model(buckets[group], model.id)
            if group == _OPENAI_RESPONSES_GROUP:
                reasoning = model.metadata.reasoning
                if reasoning is not None and reasoning.efforts:
                    efforts[_strip_model_suffix(model.id)] = tuple(reasoning.efforts)
    groups = {group: ids for group, ids in buckets.items() if ids}
    return groups, efforts, discovered_groups


def _match_override_family(
    override: str, group_specs: list[tuple[str, str, str, FamilyConfig, bool]]
) -> str | None:
    """Family group for an override that no served/default model matched exactly.

    :returns: The group key to route the override to, or ``None`` to decline.

    A slash-qualified override that reaches here names a non-gateway provider
    (a synthesized-id prefix is split off before, and a legitimate slash-bearing
    gateway id like ``zai-org/GLM-4.7`` is matched exactly before): an explicit
    ``google/...`` / ``opencode/...`` / unlisted ``anthropic/...`` selection is
    not ours to reroute, so it is declined. A bare id is routed by the family its
    name implies (Claude → anthropic, GPT/GLM → openai) when that family is
    configured; a recognized family that is NOT configured here is declined
    rather than sent to the wrong surface. An unsignalled bare id pins to a lone
    configured family (unambiguous) and is declined when several could claim it.
    """
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, OPENAI_FAMILY

    if "/" in override:
        return None
    available = {group_key for group_key, *_rest in group_specs}
    name = override.lower()
    if "claude" in name:
        return ANTHROPIC_FAMILY if ANTHROPIC_FAMILY in available else None
    if "gpt" in name or "glm" in name:
        return OPENAI_FAMILY if OPENAI_FAMILY in available else None
    if len(group_specs) == 1:
        return group_specs[0][0]
    return None


def resolve_config_gateway_providers(
    model_override: str | None = None,
    reasoning_effort: str | None = None,
) -> ConfigGatewayResolution | None:
    """Resolve the ``~/.omnigent/config.yaml`` gateway provider for opencode."""
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        CHAT_WIRE_API,
        GATEWAY_KIND,
        KEY_KIND,
        LOCAL_KIND,
        OPENAI_FAMILY,
        default_provider_for_harness,
        load_config,
    )
    from omnigent.util.reasoning_effort import EFFORT_VALUES

    effort: str | None = None
    if reasoning_effort:
        candidate = reasoning_effort.strip().lower()
        if candidate in EFFORT_VALUES and candidate != "none":
            effort = candidate

    try:
        config = load_config()
        entry = default_provider_for_harness(config, "opencode")
    except Exception:  # noqa: BLE001 - a malformed config must not break launch.
        _logger.warning(
            "opencode config gateway: failed to resolve the configured provider; "
            "falling back to the databricks/managed/default paths.",
            exc_info=True,
        )
        return None
    if entry is None or entry.kind not in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
        # subscription / databricks / cli-config / bedrock: not driveable here.
        return None

    # A gateway-qualified selection (``<provider>/<model>``) is split only once
    # the synthesized provider ids are known (below); a bare id keeps any slash
    # it carries (e.g. ``zai-org/GLM-4.7``).
    override = _strip_model_suffix(model_override) if model_override else None
    override_provider: str | None = None
    providers: dict[str, object] = {}
    auth_commands: dict[str, str] = {}

    # Resolve the driveable config families (anthropic + openai) up front.
    families: list[tuple[str, str, FamilyConfig]] = []
    for family_name, npm in (
        (ANTHROPIC_FAMILY, _AI_SDK_ANTHROPIC),
        (OPENAI_FAMILY, _AI_SDK_OPENAI_COMPATIBLE),
    ):
        try:
            family = entry.family(family_name)
        except Exception:  # noqa: BLE001 - an unresolved $VAR in an unused family.
            continue
        if family is None or not family.base_url:
            continue
        # An OpenAI family drives the chat-completions surface. Exclude it only
        # when it EXPLICITLY declares a non-chat wire (e.g. ``responses``); an
        # omitted ``wire_api`` means "harness default", which here is chat, so the
        # family is kept rather than silently dropped.
        if (
            family_name == OPENAI_FAMILY
            and family.wire_api is not None
            and family.wire_api != CHAT_WIRE_API
        ):
            continue
        families.append((family_name, npm, family))

    families_by_name = {name: family for name, _npm, family in families}

    # Successful discovery replaces stale static model lists, but only for the
    # groups whose own workspace origin was reached.
    discovered, discovered_efforts, discovered_groups = _discover_gateway_models(families)

    group_specs: list[tuple[str, str, str, FamilyConfig, bool]] = []
    if ANTHROPIC_FAMILY in families_by_name:
        group_specs.append(
            (
                ANTHROPIC_FAMILY,
                _config_gateway_provider_id(entry.name, ANTHROPIC_FAMILY),
                _AI_SDK_ANTHROPIC,
                families_by_name[ANTHROPIC_FAMILY],
                False,
            )
        )
    if OPENAI_FAMILY in families_by_name:
        openai_family = families_by_name[OPENAI_FAMILY]
        group_specs.append(
            (
                OPENAI_FAMILY,
                _config_gateway_provider_id(entry.name, OPENAI_FAMILY),
                _AI_SDK_OPENAI_COMPATIBLE,
                openai_family,
                False,
            )
        )
        # Build the Responses provider when discovery advertises Responses models,
        # OR when the session explicitly selected it (a saved
        # ``<responses>/<model>``). Its identity must survive a discovery outage:
        # a saved Responses selection is reconstructed here rather than going
        # unrecognized and falling through to another provider's default.
        responses_pid = _config_gateway_provider_id(entry.name, _OPENAI_RESPONSES_GROUP)
        override_wants_responses = bool(override) and override.split("/", 1)[0] == responses_pid
        if discovered.get(_OPENAI_RESPONSES_GROUP) or override_wants_responses:
            group_specs.append(
                (
                    _OPENAI_RESPONSES_GROUP,
                    responses_pid,
                    _AI_SDK_OPENAI,
                    openai_family,
                    True,
                )
            )

    default_source = {
        ANTHROPIC_FAMILY: ANTHROPIC_FAMILY,
        OPENAI_FAMILY: OPENAI_FAMILY,
        _OPENAI_RESPONSES_GROUP: OPENAI_FAMILY,
    }

    def _group_model_ids(group_key: str, family: FamilyConfig) -> list[str]:
        if group_key in discovered_groups:
            # Discovery reached this group's origin: authoritative (empty too).
            return list(discovered.get(group_key, ()))
        if group_key in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
            # This group's origin was not discovered; keep its configured tiers,
            # resolving alias chains (``default: pro`` → ``pro: <endpoint>``) to
            # the concrete endpoint ids the gateway actually accepts.
            resolved: list[str] = []
            for value in family.models.values():
                _append_unique_model(resolved, family.resolve_model_tier(value))
            return resolved
        return []

    # Split a gateway-qualified override (``<gateway-provider>/<model>``, a
    # synthesized id — e.g. a saved qualified ``opencode_model``). Any other
    # slash is left intact: it is either part of a gateway model id
    # (``zai-org/GLM-4.7``, matched below) or an explicit non-gateway selection
    # (``google/...``, ``opencode/...``), which the matcher declines.
    known_provider_ids = {provider_id for _gk, provider_id, *_rest in group_specs}
    if override and "/" in override:
        maybe_provider, maybe_model = override.split("/", 1)
        if maybe_provider in known_provider_ids:
            override_provider, override = maybe_provider, maybe_model

    override_group: str | None = None
    if override_provider is not None:
        override_group = next(
            (
                group_key
                for group_key, provider_id, *_rest in group_specs
                if provider_id == override_provider
            ),
            None,
        )
        if override_group is None:
            return None
        # Reconcile a picker-qualified selection with the discovered wire API: the
        # picker prefixes openai models with the chat provider, but discovery may
        # classify one as Responses. If the model is discovered in a DIFFERENT
        # group of the SAME family (chat ↔ Responses), route it there so a picker
        # entry can't pin a Responses model onto the chat endpoint.
        for group_key, _pid, _npm, family, _reasoning in group_specs:
            if (
                group_key != override_group
                and group_key in discovered_groups
                and default_source[group_key] == default_source[override_group]
                and any(
                    _strip_model_suffix(m) == override for m in _group_model_ids(group_key, family)
                )
            ):
                override_group = group_key
                break
    elif override and group_specs:
        # 1) Prefer a group whose DISCOVERED catalog lists the override: discovery
        #    knows each model's real wire API, so an explicitly selected Responses
        #    model lands on the Responses provider (with its reasoning effort),
        #    not the chat group that merely carries it as a configured default.
        for group_key, _pid, _npm, family, _reasoning in group_specs:
            if group_key in discovered_groups and any(
                _strip_model_suffix(m) == override for m in _group_model_ids(group_key, family)
            ):
                override_group = group_key
                break
        # 2) Otherwise exact-match a family's default/served models — this claims a
        #    legitimate slash-bearing gateway id such as ``zai-org/GLM-4.7``.
        if override_group is None:
            for group_key, _pid, _npm, family, _reasoning in group_specs:
                raw_default = entry.family_default_model(default_source[group_key])
                candidates = (
                    family.resolve_model_tier(raw_default) if raw_default else None,
                    *_group_model_ids(group_key, family),
                )
                if any(c and _strip_model_suffix(c) == override for c in candidates):
                    override_group = group_key
                    break
        if override_group is None:
            override_group = _match_override_family(override, group_specs)
            if override_group is None:
                # An explicit non-gateway selection, or a recognized family that
                # isn't configured here — not ours to reroute. Decline and let the
                # databricks/managed/native paths resolve it.
                return None

    override_pin: str | None = None
    anthropic_default_pin: str | None = None
    openai_default_pin: str | None = None
    first_model_pin: str | None = None
    # First model actually synthesized for each family (anthropic / openai), used
    # to exhaust the config-default family before falling to another.
    family_first_pin: dict[str, str] = {}

    for group_key, provider_id, npm, family, reasoning in group_specs:
        raw_default = entry.family_default_model(default_source[group_key])
        # Resolve an alias default (``default: pro``) to its concrete endpoint id.
        default_model = family.resolve_model_tier(raw_default) if raw_default else None
        group_ids = _group_model_ids(group_key, family)
        model_ids: list[str] = []
        if override and group_key == override_group:
            # Resolve an alias override (a saved ``<provider>/pro``) to its concrete
            # endpoint id, and strip any ``[...]`` suffix, so the pinned id matches
            # the registered model exactly (never an alias or a suffixed variant).
            resolved_override = _strip_model_suffix(family.resolve_model_tier(override))
            _append_unique_model(model_ids, resolved_override)
            override_pin = f"{provider_id}/{resolved_override}"
        if default_model:
            stripped_default = _strip_model_suffix(default_model)
            served = {_strip_model_suffix(m) for m in group_ids}
            # Add the configured default here only when this group actually serves
            # it. A discovered group is authoritative — an empty one must not claim
            # the default (so a Responses-only catalog pins Responses, not chat) —
            # and the Responses group borrows the OpenAI default only when discovery
            # confirms it is a Responses model, never as a blind fallback.
            base_fallback = (
                group_key in (ANTHROPIC_FAMILY, OPENAI_FAMILY)
                and group_key not in discovered_groups
                and not group_ids
            )
            if stripped_default in served or base_fallback:
                _append_unique_model(model_ids, default_model)
                pin = f"{provider_id}/{stripped_default}"
                if group_key == ANTHROPIC_FAMILY:
                    anthropic_default_pin = anthropic_default_pin or pin
                else:
                    openai_default_pin = openai_default_pin or pin
        for tier_model in group_ids:
            _append_unique_model(model_ids, tier_model)
        if not model_ids:
            continue

        # The Anthropic SDK appends /messages and expects /v1 in the base URL.
        base_url = family.base_url
        if npm == _AI_SDK_ANTHROPIC and not base_url.rstrip("/").endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        options: dict[str, object] = {"baseURL": base_url}
        # Dynamic credentials use a placeholder that the auth plugin replaces.
        if family.auth_command:
            auth_commands[provider_id] = family.auth_command
            options["apiKey"] = _AUTH_PLUGIN_PLACEHOLDER_KEY
        elif family.api_key:
            options["apiKey"] = family.api_key
        models_block: dict[str, object] = {}
        for mid in model_ids:
            model_entry: dict[str, object] = {"name": mid}
            if reasoning:
                model_entry["reasoning"] = True
                if effort is not None:
                    clamped = _clamp_effort(
                        effort, discovered_efforts.get(_strip_model_suffix(mid), ())
                    )
                    if clamped is not None:
                        model_entry["options"] = {"reasoningEffort": clamped}
            models_block[mid] = model_entry
        providers[provider_id] = {"npm": npm, "options": options, "models": models_block}
        if first_model_pin is None:
            first_model_pin = f"{provider_id}/{model_ids[0]}"
        family_first_pin.setdefault(default_source[group_key], f"{provider_id}/{model_ids[0]}")

    if not providers:
        return None
    # Prefer an override; otherwise pin the family the config marks this provider
    # the default FOR (a ``default: openai`` entry launches OpenAI, not Claude).
    # Exhaust that family fully — its configured default, else its first
    # discovered/served model — before considering the other family, so a stale
    # openai default that discovery replaced still launches OpenAI rather than
    # silently falling to a Claude default.
    if OPENAI_FAMILY in entry.default_families and ANTHROPIC_FAMILY not in entry.default_families:
        family_default_pin = (
            openai_default_pin
            or family_first_pin.get(OPENAI_FAMILY)
            or anthropic_default_pin
            or family_first_pin.get(ANTHROPIC_FAMILY)
        )
    else:
        family_default_pin = (
            anthropic_default_pin
            or family_first_pin.get(ANTHROPIC_FAMILY)
            or openai_default_pin
            or family_first_pin.get(OPENAI_FAMILY)
        )
    pinned = override_pin or family_default_pin or first_model_pin
    if pinned is None:
        return None

    synthesized: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": providers,
        "model": pinned,
    }
    return ConfigGatewayResolution(config=synthesized, auth_commands=auth_commands, model=pinned)


def write_opencode_provider_config(xdg_config_home: Path, config: Mapping[str, object]) -> Path:
    """
    Atomically write ``<xdg_config_home>/opencode/opencode.json`` (``0600``).

    :param xdg_config_home: The per-session ``XDG_CONFIG_HOME`` the server uses.
    :param config: The provider config dict (see
        :func:`build_opencode_provider_config`).
    :returns: The path written.
    """
    cfg_dir = xdg_config_home / "opencode"
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cfg_dir / "opencode.json"
    payload = json.dumps(config, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix="opencode.json.", dir=str(cfg_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def build_opencode_mcp_block(
    servers: Sequence[MCPServerConfig],
) -> dict[str, dict[str, object]]:
    """
    Translate Omnigent MCP server declarations into opencode.json's ``mcp`` block.

    Mirrors how codex/claude expose the agent's MCP servers, but via opencode's
    own config (no relay): ``stdio`` → ``{type:"local", command:[cmd, *args],
    environment, enabled}``; ``http`` → ``{type:"remote", url, headers,
    enabled}``. A ``databricks_profile`` resolves a bearer token into the
    ``Authorization`` header at spawn (re-resolved on resume, like the gateway
    provider). Entries opencode can't represent (missing command / url) are
    skipped.

    :param servers: The agent spec's ``mcp_servers``.
    :returns: An opencode ``mcp`` block keyed by server name (empty when none
        are representable).
    """
    block: dict[str, dict[str, object]] = {}
    for server in servers:
        name = getattr(server, "name", None)
        if not name:
            continue
        if getattr(server, "transport", "http") == "stdio":
            command = getattr(server, "command", None)
            if not command:
                continue
            entry: dict[str, object] = {
                "type": "local",
                "command": [command, *getattr(server, "args", [])],
                "enabled": True,
            }
            env = dict(getattr(server, "env", {}) or {})
            if env:
                entry["environment"] = env
        else:
            url = getattr(server, "url", None)
            if not url:
                continue
            headers = dict(getattr(server, "headers", {}) or {})
            profile = getattr(server, "databricks_profile", None)
            if profile and "Authorization" not in headers:
                token = _databricks_bearer_token(profile)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            entry = {"type": "remote", "url": url, "enabled": True}
            if headers:
                entry["headers"] = headers
        timeout = getattr(server, "timeout", None)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            # MCPServerConfig.timeout is seconds; opencode's mcp entry wants ms.
            entry["timeout"] = int(timeout * 1000)
        block[str(name)] = entry
    return block


def build_opencode_omnigent_mcp_server(
    bridge_dir: Path, *, python_executable: str | None = None
) -> dict[str, dict[str, object]]:
    """
    Build the opencode ``mcp`` entry that connects opencode to Omnigent's MCP.

    This is what makes opencode's model call the Omnigent builtin tools
    (``sys_session_*``, ``sys_agent_*``, ``load_skill``, ``web_fetch``,
    ``list_comments``/``update_comment``, policy tools, …). opencode launches the
    SHARED ``omnigent.harnesses.claude_native.bridge serve-mcp`` as a ``{type:"local"}``
    stdio MCP server (the same relay codex/cursor/qwen use); ``serve-mcp`` reads
    the relay URL+token from ``tool_relay.json`` in *bridge_dir* (written by the
    runner's comment relay) and proxies each tool call back through the Omnigent
    server, where policy is enforced. The command is sourced from
    :func:`claude_native_bridge.build_mcp_config` so the invocation stays in one
    place.

    :param bridge_dir: OpenCode-native bridge directory (must hold ``bridge.json``
        + ``tool_relay.json``).
    :param python_executable: Python to run ``serve-mcp`` with; ``None`` uses the
        runner interpreter (has ``omnigent`` importable).
    :returns: A one-entry ``mcp`` block ``{"omnigent": {type:"local", …}}``.
    """
    from omnigent.harnesses.claude_native.bridge import (
        _TOOL_RELAY_POST_TIMEOUT_S,
        build_mcp_config,
    )

    claude_cfg = build_mcp_config(bridge_dir, python_executable=python_executable)
    # build_mcp_config returns {"mcpServers": {"<name>": {command, args, env}}};
    # opencode wants a flat command list + ``environment``.
    servers = claude_cfg.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError("Claude MCP config is missing mcpServers")
    name, server = next(iter(servers.items()))
    if not isinstance(server, dict):
        raise ValueError("Claude MCP server config is malformed")
    command = server.get("command")
    args = server.get("args", [])
    if (
        not isinstance(command, str)
        or not isinstance(args, list)
        or not all(isinstance(arg, str) for arg in args)
    ):
        raise ValueError("Claude MCP server command is malformed")
    entry: dict[str, object] = {
        "type": "local",
        "command": [command, *args],
        "enabled": True,
        # opencode's mcp timeout is in MILLISECONDS and flows straight into the
        # MCP SDK's per-request deadline (default 60 s). Give the client more
        # headroom than the bridge's outer relay hop so the relay's own clean
        # timeout error always arrives before opencode kills the call. This is
        # server-wide, so a hung local (non-relay) tool also gets this window
        # before the client kills it — an accepted trade-off; local tools get
        # no heartbeat, so they stay killable at this deadline.
        "timeout": int((_TOOL_RELAY_POST_TIMEOUT_S + 30.0) * 1000),
    }
    env_value = server.get("env")
    if env_value is None:
        env: dict[str, str] = {}
    elif isinstance(env_value, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in env_value.items()
    ):
        env = dict(env_value)
    else:
        raise ValueError("Claude MCP server environment is malformed")
    if env:
        entry["environment"] = env
    return {str(name): entry}


def _databricks_bearer_token(profile: str) -> str | None:
    """Resolve a bearer token for a ``~/.databrickscfg`` profile (best-effort)."""
    try:
        from databricks.sdk.core import Config

        headers = Config(profile=profile).authenticate() or {}
        authz = headers.get("Authorization", "")
        return authz.split(" ", 1)[1] if authz.lower().startswith("bearer ") else None
    except Exception as exc:  # noqa: BLE001 - SDK absent / bad profile / auth failure.
        _logger.info("opencode MCP databricks token resolve failed for %r: %r", profile, exc)
        return None


def resolve_databricks_gateway(
    profile: str | None,
    *,
    model_id: str | None = None,
) -> OpenCodeGatewayResolution | None:
    """
    Resolve a Databricks AI gateway for opencode from a ``~/.databrickscfg`` profile.

    Uses ``databricks-sdk`` (the ``databricks`` extra) to obtain the workspace
    host + a bearer token for *profile*, then targets the workspace's
    OpenAI-compatible ``/serving-endpoints``. Best-effort: returns ``None`` when
    the SDK is absent, the profile is unknown, or auth fails — the caller then
    leaves opencode on its ambient provider config.

    :param profile: A ``~/.databrickscfg`` profile name, e.g. ``"oss"``;
        ``None`` short-circuits.
    :param model_id: Endpoint/model id to pin. When omitted or incompatible,
        the Databricks Claude catalog supplies the endpoint.
    :returns: A resolution, or ``None`` when the gateway can't be resolved.
    """
    if not profile:
        return None
    try:
        from databricks.sdk.core import Config

        config = Config(profile=profile)
        host = (config.host or "").rstrip("/")
        if not host:
            return None
        headers = config.authenticate() or {}
        authz = headers.get("Authorization", "")
        token = authz.split(" ", 1)[1] if authz.lower().startswith("bearer ") else ""
        if not token:
            return None
    except Exception as exc:  # noqa: BLE001 - SDK absent / auth failure / bad profile.
        _logger.info("opencode Databricks gateway resolve failed for %r: %r", profile, exc)
        return None

    resolved_model = _gateway_endpoint_for_model(model_id)
    if resolved_model is None:
        resolved_model = _gateway_endpoint_for_model(
            os.environ.get(DATABRICKS_GATEWAY_DEFAULT_MODEL_ENV_VAR)
        )
    if resolved_model is None:
        resolved_model = model_catalog.resolve_catalog_model(
            "databricks", family="claude"
        ).model_id
    # List every chat serving-endpoint so opencode's picker offers them all,
    # with the pinned default first. Best-effort: a failure leaves just the
    # default (dict.fromkeys de-dupes if the default is also discovered).
    model_ids = tuple(dict.fromkeys((resolved_model, *_list_gateway_models(config))))
    return OpenCodeGatewayResolution(
        base_url=f"{host}/{_SERVING_ENDPOINTS_PATH}",
        api_key=token,
        model_id=resolved_model,
        model_ids=model_ids,
    )


def _list_gateway_models(config: Config) -> tuple[str, ...]:
    """
    List the workspace's chat-capable ``databricks-*`` serving endpoints.

    Reuses the already-authenticated SDK *config* so it shares the gateway's
    auth. Best-effort: any failure (SDK absent, list denied) returns ``()`` and
    the caller falls back to the single pinned model. Embedding/rerank endpoints
    are dropped so the model picker only lists chat models.

    :param config: A ``databricks.sdk.core.Config`` for the gateway workspace.
    :returns: Endpoint names, e.g. ``("databricks-kimi-k3", ...)``.
    """
    try:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient(config=config)
        ids: list[str] = []
        for endpoint in client.serving_endpoints.list():
            name = getattr(endpoint, "name", "") or ""
            if not name.startswith("databricks-"):
                continue
            task = (getattr(endpoint, "task", "") or "").lower()
            if "embed" in task or "rerank" in task:
                continue
            ids.append(name)
        return tuple(ids)
    except Exception as exc:  # noqa: BLE001 - SDK absent / list denied / bad shape.
        _logger.info("opencode Databricks gateway model list failed: %r", exc)
        return ()


def _gateway_endpoint_for_model(model_id: str | None) -> str | None:
    """
    Normalize a spec model id to a Databricks serving-endpoint name.

    Accepts ``"databricks-claude-..."`` and ``"databricks/claude-..."`` spellings
    and strips a leading ``databricks/`` provider prefix; anything that does not
    look like a ``databricks-*`` endpoint is ignored (the gateway only routes
    its own endpoint names), so the default applies.

    :param model_id: The spec/override model id, or ``None``.
    :returns: A bare endpoint name, or ``None``.
    """
    if not model_id:
        return None
    candidate = model_id.split("/", 1)[1] if model_id.startswith("databricks/") else model_id
    return candidate if candidate.startswith("databricks-") else None


def _strip_jsonc_comments(text: str) -> str:
    """
    Strip ``//`` line comments and ``/* */`` block comments from JSONC text.

    Uses a character-level state machine to track string boundaries, so
    ``//`` inside string literals (e.g. URLs like ``"https://example.com"``)
    are never mistaken for comments.
    """
    result: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    string_char: str | None = None

    while i < length:
        ch = text[i]

        if in_string:
            if ch == "\\":
                result.append(ch)
                i += 1
                if i < length:
                    result.append(text[i])
                    i += 1
            elif ch == string_char:
                in_string = False
                result.append(ch)
                i += 1
            else:
                result.append(ch)
                i += 1
        elif ch in ('"', "'"):
            in_string = True
            string_char = ch
            result.append(ch)
            i += 1
        elif ch == "/" and i + 1 < length:
            next_ch = text[i + 1]
            if next_ch == "/":
                i += 2
                while i < length and text[i] != "\n":
                    i += 1
            elif next_ch == "*":
                i += 2
                while i + 1 < length:
                    if text[i] == "*" and text[i + 1] == "/":
                        i += 2
                        break
                    i += 1
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1

    return "".join(result)


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before ``}`` or ``]`` (valid in JSONC, invalid in JSON).

    Operates on text that has already had its JSONC comments stripped, so the
    only commas present are real JSON commas.  Uses a character-level state
    machine that tracks string boundaries so that ``, }`` or ``, ]`` inside
    quoted values are never mistaken for trailing commas — preventing silent
    corruption of provider options like ``"note": "a, }"``.
    """
    result: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    string_char: str | None = None

    while i < length:
        ch = text[i]

        if in_string:
            if ch == "\\":
                result.append(ch)
                i += 1
                if i < length:
                    result.append(text[i])
                    i += 1
            elif ch == string_char:
                in_string = False
                result.append(ch)
                i += 1
            else:
                result.append(ch)
                i += 1
        elif ch in ('"', "'"):
            in_string = True
            string_char = ch
            result.append(ch)
            i += 1
        elif ch == ",":
            j = i + 1
            while j < length and text[j] in (" ", "\t", "\n", "\r"):
                j += 1
            if j < length and text[j] in ("}", "]"):
                i = j
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1

    return "".join(result)


def maybe_merge_user_provider_config(config: dict[str, object]) -> dict[str, object]:
    """
    Merge the user's global OpenCode provider definitions into *config*.

    OpenCode reads ``XDG_CONFIG_HOME/opencode/opencode.json(c)`` for custom
    provider definitions (e.g. OpenAI-compatible endpoints with custom base
    URLs). When running under Omnigent, the per-session ``XDG_CONFIG_HOME``
    override hides this global config. This function reads the user's real
    config and merges any ``provider`` block into *config* so the spawned
    server sees both the user's providers (with their custom base URLs) and
    any Omnigent-synthesized providers (e.g. Databricks gateway).

    ``provider`` entries are merged, and the user's top-level ``plugin``
    entries are appended after any synthesized ones (synthesized policy
    plugins stay first; duplicate paths are dropped) so plugin-based
    provider auth keeps working in native sessions. The user config's
    ``model`` default is adopted **only when the synthesized config pins
    none** — for the other keys it sets (model, mcp, permission, etc.) the
    synthesized config still takes precedence. The ``model`` carry-over
    matters because when
    neither a gateway nor a spec-supplied ``model_override`` is present, the
    synthesized config has no ``model`` key, and opencode-native would otherwise
    pick its own default over the merged models map (e.g. landing on a served
    Gemini endpoint even though the user's config defaults to Claude).

    :param config: The synthesized config dict (may be empty).
    :returns: *config* with user's ``provider`` entries (and, if unset, the
        user's default ``model``) merged in.
    """
    from omnigent.harnesses.opencode_native.bridge import user_opencode_config_path

    user_path = user_opencode_config_path()
    if user_path is None:
        return config

    try:
        raw = user_path.read_text(encoding="utf-8")
        # Try plain JSON first (handles .json files without comments).
        # If that fails, strip JSONC comments and trailing commas, then
        # retry (handles .jsonc).
        try:
            user_config = json.loads(raw)
        except json.JSONDecodeError:
            cleaned = _strip_jsonc_comments(raw)
            cleaned = _strip_trailing_commas(cleaned)
            user_config = json.loads(cleaned)
    except (OSError, UnicodeDecodeError):
        return config
    except json.JSONDecodeError:
        _logger.warning(
            "Failed to parse user OpenCode config at %s — ignoring user providers",
            user_path,
        )
        return config

    if not isinstance(user_config, dict):
        return config

    # Adopt the user's default ``model`` when the synthesized config pins none.
    # ``setdefault`` keeps the synthesized value authoritative (gateway /
    # spec-supplied ``model_override`` win); it only fills the gap where both
    # were absent, so opencode-native launches on the user's chosen default
    # instead of picking its own over the merged models map.
    def _carry_model(target: dict[str, object]) -> None:
        user_model = user_config.get("model")
        if isinstance(user_model, str) and user_model:
            target.setdefault("model", user_model)

    def _merge_plugins(target: dict[str, object]) -> None:
        """Preserve user plugins while retaining synthesized policy hooks."""
        user_plugins = user_config.get("plugin")
        if not isinstance(user_plugins, list):
            return
        existing = target.get("plugin")
        merged: list[object] = list(existing) if isinstance(existing, list) else []
        for plugin in user_plugins:
            # Plugin entries are paths/identifiers; skip anything that isn't a
            # non-empty string so we never emit a config OpenCode would reject.
            if not isinstance(plugin, str) or not plugin:
                continue
            if plugin not in merged:
                merged.append(plugin)
        if merged:
            target["plugin"] = merged

    user_providers = user_config.get("provider")
    if not isinstance(user_providers, dict) or not user_providers:
        # No custom providers to merge, but the user's default model still
        # applies when the synthesized config didn't pin one.
        result = dict(config)
        _carry_model(result)
        _merge_plugins(result)
        return result

    result = dict(config)
    existing = result.get("provider")
    if isinstance(existing, dict):
        # Merge user's providers alongside existing ones; don't clobber
        # synthesized providers (Omnigent's keys like "databricks-gateway"
        # take priority).
        merged = dict(existing)
        for key, value in user_providers.items():
            if key not in merged:
                merged[key] = value
        result["provider"] = merged
    else:
        result["provider"] = dict(user_providers)

    _carry_model(result)
    _merge_plugins(result)
    result.setdefault("$schema", "https://opencode.ai/config.json")

    return result


def _configure_opencode_on_demand() -> None:
    """Run ``ucode configure --agents opencode`` for the connect profile, minting
    via the broker. Used when the background boot configure has not yet written
    opencode's config at launch time. Best-effort and quiet."""
    from omnigent.host.databricks_credential import HOST_DATABRICKS_PROFILE, broker_token_command
    from omnigent.inner.databricks_executor import _read_databrickscfg_host
    from omnigent.onboarding.ucode_setup import (
        build_ucode_configure_command_for_profile,
        find_ucode_command,
        ucode_configure_lock,
    )

    host = _read_databrickscfg_host(HOST_DATABRICKS_PROFILE)
    bearer_command = broker_token_command(host.rstrip("/")) if host else None
    if not bearer_command:
        return
    ucode_config = Path.home() / ".ucode" / "opencode-xdg" / "opencode" / "opencode.json"
    try:
        with ucode_configure_lock():
            # The boot-time all-agent configure may have written opencode's config
            # while we waited for the lock — skip a redundant run if so.
            if ucode_config.exists():
                return
            argv = build_ucode_configure_command_for_profile(
                find_ucode_command(), profile=HOST_DATABRICKS_PROFILE, agents=["opencode"]
            )
            configure_env = {
                **os.environ,
                "DATABRICKS_BEARER_COMMAND": bearer_command,
                "DATABRICKS_CONFIG_PROFILE": HOST_DATABRICKS_PROFILE,
            }
            # ucode has no use for the host's launch token; don't hand it over.
            configure_env.pop("OMNIGENT_HOST_TOKEN", None)
            subprocess.run(argv, capture_output=True, timeout=120, env=configure_env)
    except Exception:  # noqa: BLE001 - best-effort; the caller declines if config is still absent.
        _logger.info("opencode on-demand ucode configure failed", exc_info=True)


def _provider_base_urls_match_host(config: Mapping[str, object], workspace_host: str) -> bool:
    """True when every opencode provider base URL is HTTPS and shares
    *workspace_host*'s network location.

    Guards the managed-connect path: a broker bearer is forwarded to whatever
    ``provider.<id>.options.baseURL`` the on-disk config names, so a stale/other
    origin must not be trusted. Requires at least one base URL (a provider block
    with none is not a usable gateway target).
    """
    from omnigent.host.databricks_credential import https_url_on_workspace_host

    providers = config.get("provider")
    if not isinstance(providers, Mapping):
        return False
    saw_url = False
    for provider in providers.values():
        options = provider.get("options") if isinstance(provider, Mapping) else None
        base_url = options.get("baseURL") if isinstance(options, Mapping) else None
        if not isinstance(base_url, str) or not base_url:
            continue
        saw_url = True
        if not https_url_on_workspace_host(base_url, workspace_host):
            return False
    return saw_url


def managed_connect_opencode_config(xdg_config_home: Path) -> dict[str, object] | None:
    """Consume ucode's generated opencode config on a managed connect host.

    The opencode counterpart to Claude reading ``read_ucode_state``: on a managed
    connect host, ``ucode configure --agents opencode`` (run at host boot) writes
    ``~/.config/opencode/opencode.json`` (provider block + served-model selectors)
    and a ``plugin/ucode-auth.js`` that mints a fresh Databricks token per request
    via ``ucode auth-token`` (→ the broker). omnigent isolates opencode to a
    per-session ``XDG_CONFIG_HOME``, so this reuses ucode's output: it returns
    ucode's config (to seed the session ``opencode.json``) after copying the auth
    plugin into the session plugin dir and pointing ``plugin`` at the copy.

    Reuse over reinvention — the same ucode artifact serves OSS connect sandboxes
    here, lakebox (via ``ucode opencode``), and ucode's own users; refresh comes
    from ucode's plugin, not a static omnigent-minted token.

    Returns ``None`` off a managed connect host (no broker sidecar) or when ucode
    did not generate an opencode config — so laptop and non-connect launches are
    untouched. The caller must also forward ``DATABRICKS_BEARER_COMMAND`` into the
    opencode process env so the plugin's ``ucode auth-token`` can mint.
    """
    from omnigent.host.databricks_credential import _read_sidecar, _sidecar_path

    sidecar = _read_sidecar(_sidecar_path())
    if sidecar is None:
        return None  # not a managed connect host
    workspace_host = sidecar["workspace_host"].rstrip("/")

    # ucode writes opencode's config into its own XDG root, not ~/.config/opencode.
    ucode_config_dir = Path.home() / ".ucode" / "opencode-xdg" / "opencode"
    ucode_config = ucode_config_dir / "opencode.json"
    if not ucode_config.exists():
        # The boot-time configure_ucode_for_sandbox runs in the background and may
        # not have written opencode's config yet when this launch resolves. Unlike
        # claude/codex/pi, opencode has no working hand-built fallback on the
        # connect path, so configure it on demand here (a few seconds, off the
        # runner's dial-back path). Best-effort; a failure just declines below.
        _configure_opencode_on_demand()
    try:
        config = json.loads(ucode_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.info("opencode managed config: unreadable ucode config %s: %r", ucode_config, exc)
        return None
    if not isinstance(config, dict) or "provider" not in config:
        _logger.info(
            "opencode managed config: ucode did not configure opencode (no provider block in %s); "
            "opencode falls back to its own login.",
            ucode_config,
        )
        return None  # ucode did not configure opencode (e.g. not in --agents)
    # Security: the config on disk carries the provider base URL, and we forward a
    # freshly-minted broker bearer to it. A stale config (left from a previous
    # workspace connection) or a locally-modified file could aim that bearer at a
    # different origin. Only trust it when every provider base URL is HTTPS and
    # targets the sidecar's current workspace host.
    if not _provider_base_urls_match_host(config, workspace_host):
        _logger.warning(
            "opencode managed config: provider base URL is not HTTPS on the connected "
            "workspace host %r — declining so the broker bearer is not forwarded to an "
            "unverified origin.",
            workspace_host,
        )
        return None

    ucode_plugin = ucode_config_dir / "plugin" / "ucode-auth.js"
    try:
        plugin_src = ucode_plugin.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.info(
            "opencode managed config: provider block present but the refresh plugin %s is "
            "unreadable (%r); declining so no static bearer is used.",
            ucode_plugin,
            exc,
        )
        return None  # provider block without the refresh plugin is not usable
    session_plugin_dir = xdg_config_home / "opencode" / "plugin"
    session_plugin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_plugin = session_plugin_dir / "ucode-auth.js"
    session_plugin.write_text(plugin_src, encoding="utf-8")
    # Point at the session copy; the caller appends its own policy plugin.
    config["plugin"] = [str(session_plugin)]
    return config
