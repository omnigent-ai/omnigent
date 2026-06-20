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
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LITELLM_KIND,
    LOCAL_KIND,
    OPENAI_FAMILY,
    PI_SURFACE,
    ProviderEntry,
    get_default_provider,
    load_config,
)

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

# Databricks AI Gateway Anthropic Messages surface. Pi speaks this protocol
# natively (``api: anthropic-messages``); the gateway authenticates with a
# workspace bearer token, so we set ``authHeader`` (Authorization: Bearer).
_DATABRICKS_ANTHROPIC_GATEWAY_PATH = "/ai-gateway/anthropic"


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

    def to_models_config(self) -> dict[str, Any]:
        """Render this provider as a Pi ``models.json`` mapping."""
        provider: dict[str, Any] = {
            "baseUrl": self.base_url,
            "api": self.api,
            "apiKey": self.api_key,
            "models": [{"id": self.model}],
        }
        if self.auth_header:
            provider["authHeader"] = True
        return {"providers": {self.provider_id: provider}}


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
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=f"{host}{_DATABRICKS_ANTHROPIC_GATEWAY_PATH}",
        api="anthropic-messages",
        model=model or _DATABRICKS_PI_DEFAULT_MODEL,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token is refreshed per request (the auth command itself
        # force-refreshes), matching codex-native's refresh semantics.
        api_key=f"!{auth_command}",
        auth_header=True,
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
    for family_name, api in (("anthropic", "anthropic-messages"), ("openai", "openai-responses")):
        family = entry.family(family_name)
        if family is None or not family.base_url:
            continue
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
        # for ``pi``. Prefer an explicit pi default, then Anthropic (Pi's native
        # surface), then OpenAI.
        entry = (
            get_default_provider(config, PI_SURFACE)
            or get_default_provider(config, ANTHROPIC_FAMILY)
            or get_default_provider(config, OPENAI_FAMILY)
        )
        if entry is None:
            return None
        if entry.kind == DATABRICKS_KIND:
            return _databricks_pi_provider(entry, model=model)
        if entry.kind in (KEY_KIND, GATEWAY_KIND, LITELLM_KIND, LOCAL_KIND):
            return _inline_family_pi_provider(entry, model=model)
        # subscription / cli-config: a CLI's own login can't be reused outside
        # that CLI — let Pi use its own login.
        return None
    except Exception:  # noqa: BLE001 — any resolution failure must not break launch
        # Any failure (malformed config, duplicate per-family default, or an
        # unresolved ``api_key: $VAR``) falls back to Pi's own login rather than
        # failing the terminal launch.
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
    args = ["--provider", provider.provider_id, "--model", provider.model]
    return env, args
