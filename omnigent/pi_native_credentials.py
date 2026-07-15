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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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

# Default model for the Databricks AI Gateway's Anthropic surface — the same
# default the in-process Databricks executor pins. Used when the session
# carries no explicit model override.
_DATABRICKS_PI_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"

# Provider id for the secondary OpenAI Completions provider registered alongside
# the primary Anthropic provider in Databricks gateway configs.
_PI_OPENAI_PROVIDER_ID = "omnigent-openai"

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

# Trusted parent domain suffixes for a Databricks-owned host. The AI Gateway
# lives under a per-workspace subdomain of one of these (the canonical form is
# ``<workspace>.ai-gateway.cloud.databricks.com``); the Azure / GCP control
# planes serve workspaces under their own parent domains. We anchor on the
# leading "." so a look-alike like ``...cloud.databricks.com.evil.test`` (which
# ends in ``.evil.test``) is rejected.
_DATABRICKS_TRUSTED_HOST_SUFFIXES = (
    ".cloud.databricks.com",  # AWS workspaces + ai-gateway (incl. *.staging.cloud.databricks.com)
    ".azuredatabricks.net",  # Azure Databricks
    ".gcp.databricks.com",  # GCP Databricks
)

# A genuine AI Gateway host carries the ``ai-gateway`` DNS label; we require it
# (alongside a trusted suffix) so a non-gateway Databricks host isn't routed as
# the gateway's Anthropic surface.
_DATABRICKS_AI_GATEWAY_LABEL = "ai-gateway"


def _is_databricks_ai_gateway_url(base_url: str) -> bool:
    """Return ``True`` only for a genuine Databricks AI Gateway base URL.

    Two URL shapes are accepted:

    1. **Dedicated AI Gateway subdomain** — ``ai-gateway`` is a full DNS label
       in the hostname (e.g. ``<id>.ai-gateway.cloud.databricks.com``). Used by
       the standard ``isaac configure codex`` setup.
    2. **Workspace-hosted gateway** — the hostname is a plain Databricks
       workspace (ends with a trusted suffix) and the path starts with
       ``/ai-gateway/`` (e.g. ``<workspace>.cloud.databricks.com/ai-gateway/...``).
       Used by ucode / Codex app profile setups.

    Both cases require ``https`` and a hostname ending with a trusted
    Databricks-owned domain suffix to prevent token-forwarding attacks.

    :param base_url: The codex provider table's ``base_url``.
    :returns: ``True`` iff the URL is an https Databricks AI Gateway endpoint.
    """
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    hostname = hostname.lower()
    trusted = any(hostname.endswith(suffix) for suffix in _DATABRICKS_TRUSTED_HOST_SUFFIXES)
    if not trusted:
        return False
    # Shape 1: ``ai-gateway`` is a full DNS label in the hostname.
    labels = hostname.split(".")
    if _DATABRICKS_AI_GATEWAY_LABEL in labels:
        return True
    # Shape 2: workspace hostname + /ai-gateway/ path prefix.
    path = parsed.path or ""
    return path.startswith("/ai-gateway/")


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
    """

    provider_id: str
    base_url: str
    api: str
    model: str
    api_key: str
    auth_header: bool
    # Full model list for providers that expose multiple models (e.g. the
    # Databricks Anthropic gateway). Excluded from __hash__ so the frozen
    # dataclass stays hashable even though list[dict] is not hashable.
    extra_models: list[dict[str, Any]] = field(default_factory=list, hash=False)
    # Extra providers to merge into models.json alongside the primary one (e.g.
    # an OpenAI Completions provider for GPT models on the Databricks gateway).
    # Keys are provider ids; values are complete Pi provider config dicts.
    additional_providers: dict[str, Any] = field(default_factory=dict, hash=False)

    def to_models_config(self) -> dict[str, Any]:
        """Render this provider as a Pi ``models.json`` mapping."""
        if self.extra_models:
            # Include all known models, ensuring the selected model is present.
            # The selected model may be a newer id not yet in the static list.
            models: list[dict[str, Any]] = list(self.extra_models)
            # Only append to this (Anthropic) provider when the model is absent
            # from ALL providers. Non-Claude models (GLM, GPT…) live in
            # additional_providers (openai-completions); appending them here
            # too would register them under the wrong wire protocol.
            in_additional = any(
                any(m.get("id") == self.model for m in prov.get("models", []))
                for prov in self.additional_providers.values()
            )
            if not any(m.get("id") == self.model for m in models) and not in_additional:
                models.append({"id": self.model, "input": ["text", "image"]})
        else:
            models = [{"id": self.model}]
        provider: dict[str, Any] = {
            "baseUrl": self.base_url,
            "api": self.api,
            "apiKey": self.api_key,
            "models": models,
        }
        if self.auth_header:
            provider["authHeader"] = True
        providers: dict[str, Any] = {self.provider_id: provider}
        providers.update(self.additional_providers)
        return {"providers": providers}


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
    try:
        from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

        creds = resolve_databricks_workspace(entry.profile)
        claude_models, openai_models = _fetch_pi_model_lists(creds.host, creds.token)
    except Exception:  # noqa: BLE001 — credential/network failure must not break launch
        _LOGGER.info(
            "pi-native: falling back to single-model display (could not resolve credentials)"
        )
        claude_models = []
        openai_models = []
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=f"{host}{_DATABRICKS_ANTHROPIC_GATEWAY_PATH}",
        api="anthropic-messages",
        model=model or _DATABRICKS_PI_DEFAULT_MODEL,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token is refreshed per request (the auth command itself
        # force-refreshes), matching codex-native's refresh semantics.
        api_key=api_key,
        auth_header=True,
        extra_models=claude_models,
        additional_providers=(
            {
                _PI_OPENAI_PROVIDER_ID: _databricks_openai_provider(
                    api_key, f"{host}/serving-endpoints", openai_models
                )
            }
            if openai_models
            else {}
        ),
    )


def _databricks_openai_provider(
    api_key: str,
    serving_endpoints_url: str,
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a Pi OpenAI Completions provider config for the Databricks gateway.

    GPT models on the Databricks workspace are served via the OpenAI
    Completions API at ``/serving-endpoints``. The ``compat`` block disables
    OpenAI-specific features the Databricks endpoint doesn't support.
    """
    return {
        "baseUrl": serving_endpoints_url,
        "apiKey": api_key,
        "api": "openai-completions",
        "compat": {
            "supportsDeveloperRole": False,
            "supportsStore": False,
            "supportsStrictMode": False,
            "supportsReasoningEffort": False,
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
    import shlex
    import subprocess

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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch live model lists from the Databricks serving-endpoints API.

    Calls ``GET <workspace>/api/2.0/serving-endpoints``, filters for READY LLM
    endpoints, and splits them into two Pi model entry dict lists:

    * Claude models → ``anthropic-messages`` provider.
    * All other LLMs (GPT, GLM, Llama, Qwen, Kimi, …) → ``openai-completions``
      provider. All non-Claude Databricks LLMs share the same serving-endpoints
      URL and wire protocol, so a single provider covers them all.

    Falls back to empty lists on any HTTP or auth failure so a network blip
    never breaks Pi session launch.

    :param workspace_url: Databricks workspace base URL, e.g.
        ``"https://wkspc.example.com"`` — **no** trailing slash or path.
    :param token: Bearer token for the workspace API.
    :returns: ``(claude_models, openai_models)`` — Pi model entry dicts ready
        to write into ``models.json``.
    """
    import httpx

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{workspace_url.rstrip('/')}/api/2.0/serving-endpoints",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001 — HTTP/network failure → empty
        _LOGGER.warning(
            "pi-native: could not fetch Databricks model list; "
            "Pi will show only the selected model",
            exc_info=True,
        )
        return [], []

    endpoints = payload.get("endpoints") if isinstance(payload, dict) else None
    claude: list[dict[str, Any]] = []
    # All non-Claude LLM models (GPT, GLM, Llama, Qwen, Kimi, …) route to the
    # same OpenAI Completions provider and serving-endpoints URL, so they share
    # one list regardless of model family.
    openai: list[dict[str, Any]] = []

    for endpoint in endpoints if isinstance(endpoints, list) else []:
        if not isinstance(endpoint, dict):
            continue
        name = endpoint.get("name")
        if not isinstance(name, str) or not name:
            continue
        # Filter to chat/completion LLM endpoints (exclude embeddings/rerankers).
        task = endpoint.get("task", "")
        task_lower = task.lower() if isinstance(task, str) else ""
        name_lower = name.lower()
        if task_lower:
            is_llm = any(t in task_lower for t in ("chat", "completion"))
        else:
            is_llm = any(
                t in name_lower
                for t in ("claude", "gpt", "codex", "gemini", "llama", "qwen", "kimi", "glm")
            )
        if not is_llm:
            continue
        state = endpoint.get("state")
        ready = state.get("ready") if isinstance(state, dict) else None
        if isinstance(ready, str) and ready and ready.upper() != "READY":
            continue
        entry: dict[str, Any] = {"id": name, "input": ["text", "image"]}
        if "claude" in name_lower:
            claude.append(entry)
        else:
            openai.append(entry)

    if not claude and not openai:
        _LOGGER.info(
            "pi-native: Databricks serving-endpoints returned no LLM models; "
            "Pi will show only the selected model"
        )

    return claude, openai


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
            from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

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
    claude_models: list[dict[str, Any]] = []
    openai_models: list[dict[str, Any]] = []
    # Derive the workspace URL for the serving-endpoints API call.
    # For dedicated-subdomain URLs (ai-gateway.cloud.databricks.com), the
    # real workspace hostname must come from ~/.databrickscfg. For
    # workspace-hosted gateway URLs (workspace.cloud.databricks.com/ai-gateway/),
    # the transport's own hostname IS the workspace.
    parsed_gateway = urlparse(transport.base_url)
    gateway_labels = (parsed_gateway.hostname or "").split(".")
    if _DATABRICKS_AI_GATEWAY_LABEL in gateway_labels:
        # Dedicated subdomain: derive workspace from ~/.databrickscfg DEFAULT.
        real_workspace_url: str | None = None
        try:
            from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

            real_workspace_url = resolve_databricks_workspace(None).host
        except Exception:  # noqa: BLE001 — no .databrickscfg → skip listing
            _LOGGER.info(
                "pi-native: cli-config path could not resolve workspace URL "
                "for model listing; Pi will show only the selected model"
            )
    else:
        # Workspace-hosted gateway: the transport hostname is the workspace.
        real_workspace_url = f"https://{parsed_gateway.hostname}"
    if real_workspace_url and transport.auth_command:
        token = _run_auth_command(transport.auth_command)
        if token:
            claude_models, openai_models = _fetch_pi_model_lists(real_workspace_url, token)
        else:
            _LOGGER.info(
                "pi-native: auth command produced no token; Pi will show only the selected model"
            )
    additional: dict[str, Any] = (
        {
            _PI_OPENAI_PROVIDER_ID: _databricks_openai_provider(
                api_key, f"{real_workspace_url}/serving-endpoints", openai_models
            )
        }
        if real_workspace_url and openai_models
        else {}
    )
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=_gateway_anthropic_base_url(transport.base_url),
        api="anthropic-messages",
        model=model or _DATABRICKS_PI_DEFAULT_MODEL,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token (the codex auth command prints it) is refreshed per
        # request — matching codex-native's refresh semantics.
        api_key=api_key,
        auth_header=True,
        extra_models=claude_models,
        additional_providers=additional,
    )


def _inline_family_pi_provider(
    entry: ProviderEntry, *, model: str | None
) -> PiProviderConfig | None:
    """Resolve a key/gateway/local provider into Pi config from its family.

    Prefers the Anthropic family (Pi speaks ``anthropic-messages`` natively),
    falling back to the OpenAI family via the Responses API.

    :param entry: The resolved default provider entry.
    :param model: Session model override, or ``None`` to use the family default.
    :returns: The Pi provider config, or ``None`` when no usable family with a
        base URL and credential is configured.
    """
    for family_name in ("anthropic", "openai"):
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
    config_loader: Callable[[], dict[str, Any]] = load_config,
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

            providers = config.get("providers") or {}
            db_entry = next(
                (
                    _parse_provider(name, raw)  # type: ignore[arg-type]
                    for name, raw in (providers.items() if isinstance(providers, dict) else [])
                    if isinstance(raw, dict) and raw.get("kind") == DATABRICKS_KIND
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


def write_pi_models_config(agent_dir: Path, provider: PiProviderConfig) -> Path:
    """Write *provider* as ``models.json`` into a managed Pi config dir.

    :param agent_dir: The managed Pi config dir (``PI_CODING_AGENT_DIR``).
    :param provider: The resolved provider config to render.
    :returns: Path to the written ``models.json``.
    """
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent_dir, 0o700)
    models_path = agent_dir / "models.json"
    # 0o600: the apiKey may be a literal token (key-kind providers).
    fd = os.open(models_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(provider.to_models_config(), handle, indent=2, sort_keys=True)
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
    write_pi_models_config(agent_dir, provider)
    env = {PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir)}
    # Resolve which provider the selected model lives in. Non-Claude models
    # (GLM, GPT, Llama…) are in additional_providers (omnigent-openai);
    # Claude models are in the primary provider (omnigent). Pass the correct
    # --provider so Pi can resolve the model id.
    model_provider_id = provider.provider_id
    for extra_id, extra_cfg in provider.additional_providers.items():
        if any(m.get("id") == provider.model for m in extra_cfg.get("models", [])):
            model_provider_id = extra_id
            break
    args = ["--provider", model_provider_id, "--model", provider.model]
    return env, args
