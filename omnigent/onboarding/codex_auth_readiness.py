"""Read Codex's effective provider auth without spawning the Codex CLI.

The readiness verdict mirrors Codex's ``auth.credentials`` check: a provider
that requires OpenAI auth uses ``auth.json``; a provider with ``env_key`` is
ready only when that variable is populated; and any other non-OpenAI provider
handles auth itself. This deliberately remains a local, structural check. It
may be wrong about token validity, but it is cheap enough for the host's
periodic readiness refresh and never probes the network.

This module also owns the shared ``config.toml`` parsing primitives used by
ambient provider adoption. Adoption and readiness ask different questions:
adoption requires self-contained auth, while readiness asks how Codex itself
would authenticate the selected provider.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import tomllib

CODEX_BUILTIN_PROVIDERS = frozenset(
    {"openai", "amazon-bedrock", "amazon-bedrock-runtime", "ollama", "lmstudio"}
)
_CODEX_AUTH_HEADER_NAMES = frozenset({"authorization", "api-key", "x-api-key"})

CodexConfigAuth = Literal["provider-ready", "provider-auth-missing", "codex-login"]


def load_codex_config(config_path: Path) -> dict[str, object] | None:
    """Parse a Codex ``config.toml``, returning ``None`` when unusable."""
    try:
        raw = config_path.read_bytes()
    except OSError:
        return None
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def effective_codex_model_provider(config: dict[str, object]) -> str | None:
    """Resolve Codex's effective provider (active profile before top-level)."""
    provider_id: object = config.get("model_provider")
    active_profile = config.get("profile")
    if isinstance(active_profile, str) and active_profile.strip():
        profiles = config.get("profiles")
        if isinstance(profiles, dict):
            profile_table = profiles.get(active_profile)
            if isinstance(profile_table, dict) and isinstance(
                profile_table.get("model_provider"), str
            ):
                provider_id = profile_table["model_provider"]
    if isinstance(provider_id, str) and provider_id.strip():
        return provider_id
    return None


def effective_custom_provider_table(
    config: dict[str, object],
) -> tuple[str, dict[str, object]] | None:
    """Return the effective custom provider id and table, when both exist."""
    provider_id = effective_codex_model_provider(config)
    if provider_id is None or provider_id in CODEX_BUILTIN_PROVIDERS:
        return None
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    table = providers.get(provider_id)
    if not isinstance(table, dict):
        return None
    return provider_id, table


def provider_table_has_self_contained_auth(table: dict[str, object]) -> bool:
    """Return whether a custom provider table carries adoption-safe auth.

    Env-based auth is intentionally excluded: adopting such a provider would
    require forwarding an arbitrary variable through the scrubbed Codex launch
    environment. Readiness handles ``env_key`` separately because Codex keeps
    its own provider config on that path.
    """
    auth = table.get("auth")
    if isinstance(auth, dict):
        command = auth.get("command")
        if isinstance(command, str) and command.strip():
            return True
    bearer = table.get("experimental_bearer_token")
    if isinstance(bearer, str) and bearer.strip():
        return True
    if isinstance(table.get("aws"), dict):
        return True
    headers = table.get("http_headers")
    if isinstance(headers, dict):
        return any(
            isinstance(key, str)
            and key.lower() in _CODEX_AUTH_HEADER_NAMES
            and isinstance(value, str)
            and value.strip()
            for key, value in headers.items()
        )
    return False


def codex_config_effective_auth(
    config_path: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> CodexConfigAuth:
    """Return how Codex authenticates its effective provider.

    Mirrors Codex's provider-specific auth check:

    * unset / built-in ``openai`` / ``requires_openai_auth = true`` →
      ``"codex-login"`` (inspect ``auth.json``);
    * non-OpenAI provider declaring ``env_key`` → ``"provider-ready"`` only
      when the variable is non-empty, otherwise ``"provider-auth-missing"``;
    * any other non-OpenAI provider → ``"provider-ready"`` because Codex
      considers provider-specific auth sufficient or unnecessary.

    Built-in provider definitions in ``[model_providers]`` are ignored here,
    matching Codex's catalog merge (except Bedrock's supported endpoint/auth
    overrides, which do not change this verdict). For example, Ollama needs no
    credential; Bedrock may still fail later if its AWS credential chain is
    unavailable, which is intentionally outside this local status check.

    ``requires_openai_auth`` intentionally wins over ``env_key`` when both are
    set, matching Codex's readiness diagnostic for that contradictory config.

    The caller should pass the environment the launch will actually receive.
    Host-wide readiness has no agent spec, so it cannot include a particular
    spec's ``sandbox.env_passthrough`` additions; such a custom session may work
    while the host-level picker conservatively reports ``needs-auth``.
    """
    config = load_codex_config(config_path)
    if config is None:
        return "codex-login"

    provider_id = effective_codex_model_provider(config)
    if provider_id is None:
        return "codex-login"

    # Configured entries cannot replace the built-in OpenAI, Ollama, or LM
    # Studio providers. Bedrock accepts only limited endpoint/auth overrides;
    # none changes whether it requires Codex login.
    if provider_id in CODEX_BUILTIN_PROVIDERS:
        return "codex-login" if provider_id == "openai" else "provider-ready"

    providers = config.get("model_providers")
    configured_table = providers.get(provider_id) if isinstance(providers, dict) else None
    table = configured_table if isinstance(configured_table, dict) else None
    if table is None:
        # A selected custom provider without a table is invalid. Do not let an
        # unrelated auth.json credential mask the broken selection.
        return "provider-auth-missing"

    if table.get("requires_openai_auth") is True:
        return "codex-login"

    env_key = table.get("env_key")
    if not isinstance(env_key, str) or not env_key.strip():
        return "provider-ready"
    value = (os.environ if env is None else env).get(env_key.strip())
    if isinstance(value, str) and value.strip():
        return "provider-ready"
    return "provider-auth-missing"
