"""OpenCode readiness + credential reporting for ``omnigent setup``.

OpenCode owns its own provider auth via ``opencode auth login`` (stored in
``~/.local/share/opencode/auth.json``) or ambient provider env vars
(``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / …). Omnigent stores exactly one
optional credential of its own: the OpenCode Zen API key, kept in Omnigent's
keychain and injected as ``OPENCODE_API_KEY`` at spawn (see
:mod:`omnigent.opencode_zen_credentials`) — it never touches ``auth.json``.
This module is otherwise a thin, read-only reporter so ``omnigent setup``
can show which providers OpenCode can reach and offer to run its native
login — without ever touching its secrets.

It reads ``auth.json`` directly (a JSON object keyed by provider id — see
``packages/opencode/src/auth`` in the OpenCode source) rather than scraping
``opencode auth list`` output, and checks a curated set of common provider env
vars. Both are best-effort: a missing/unreadable file or unknown env var simply
reports "nothing configured", never raises.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.harness_install import OPENCODE_KEY, harness_cli_installed
from omnigent.opencode_zen_credentials import resolve_opencode_zen_key

# Common OpenCode providers → (provider id, display label, env var). The
# provider id matches OpenCode's own id (the ``auth.json`` key and the
# ``provider/model`` prefix in ``opencode models``). Not exhaustive (OpenCode
# resolves many providers from models.dev); this is the set worth surfacing in
# setup, including the ``OPENAI_*`` pair the Databricks-gateway path uses.
_ENV_PROVIDER_VARS: tuple[tuple[str, str, str], ...] = (
    ("openai", "OpenAI", "OPENAI_API_KEY"),
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
    ("google", "Google Gemini", "GEMINI_API_KEY"),
    ("google", "Google Gemini", "GOOGLE_GENERATIVE_AI_API_KEY"),
    ("groq", "Groq", "GROQ_API_KEY"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY"),
    ("xai", "xAI", "XAI_API_KEY"),
    ("mistral", "Mistral", "MISTRAL_API_KEY"),
    ("deepseek", "DeepSeek", "DEEPSEEK_API_KEY"),
)


def opencode_auth_path() -> Path:
    """Return OpenCode's ``auth.json`` path for this process's HOME.

    Honors ``XDG_DATA_HOME``; defaults to ``~/.local/share/opencode/auth.json``
    (OpenCode's ``Global.Path.data``).
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode" / "auth.json"


def _stored_providers() -> tuple[str, ...]:
    """Return provider ids with stored credentials in ``auth.json``.

    Best-effort: a missing/unreadable/non-object file yields ``()``. OpenCode's
    auth file is a provider-id keyed JSON object; an empty object value is not a
    credential, so ignore it rather than reporting a false ready state from a
    structural shell like ``{"openai": {}}``.
    """
    try:
        data = json.loads(opencode_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    return tuple(str(k) for k, v in data.items() if bool(v))


def _env_providers(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Return provider labels whose API-key env var is present."""
    env = os.environ if environ is None else environ
    seen: list[str] = []
    for _provider_id, label, var in _ENV_PROVIDER_VARS:
        if env.get(var, "").strip() and label not in seen:
            seen.append(label)
    return tuple(seen)


def reachable_provider_ids(environ: dict[str, str] | None = None) -> frozenset[str]:
    """Return OpenCode provider ids reachable from stored auth + env keys.

    Ids match OpenCode's own (the ``provider/model`` prefix), so callers can
    filter a model list down to what the user can actually authenticate.
    """
    env = os.environ if environ is None else environ
    ids = set(_stored_providers())
    for provider_id, _label, var in _ENV_PROVIDER_VARS:
        if env.get(var, "").strip():
            ids.add(provider_id)
    # The Zen key (env or Omnigent keychain) authenticates OpenCode's own
    # ``opencode`` provider, so its models are reachable too.
    if resolve_opencode_zen_key(environ) is not None:
        ids.add("opencode")
    return frozenset(ids)


@dataclass(frozen=True)
class OpenCodeAuthSummary:
    """What setup needs to know about the local OpenCode credentials.

    :param installed: ``opencode`` binary present on ``PATH``.
    :param stored_providers: Provider ids with credentials in ``auth.json``.
    :param env_providers: Provider labels whose API-key env var is set.
    :param zen_key_source: Where the OpenCode Zen API key resolves from
        (``"env:<NAME>"`` or ``"keychain"``), or ``None`` when absent.
    """

    installed: bool
    stored_providers: tuple[str, ...]
    env_providers: tuple[str, ...]
    zen_key_source: str | None = None

    @property
    def has_provider(self) -> bool:
        """Whether any provider is reachable (stored, env key, or Zen key)."""
        return bool(self.stored_providers or self.env_providers or self.zen_key_source)

    @property
    def ready(self) -> bool:
        """Launchable when the CLI is installed AND a provider is configured."""
        return self.installed and self.has_provider

    def describe(self) -> str:
        """A short human summary of configured providers, e.g.
        ``"2 stored (anthropic, openai) + env: OpenAI"``.
        """
        parts: list[str] = []
        if self.stored_providers:
            parts.append(
                f"{len(self.stored_providers)} stored ({', '.join(sorted(self.stored_providers))})"
            )
        if self.env_providers:
            parts.append(f"env: {', '.join(self.env_providers)}")
        if self.zen_key_source:
            parts.append(f"zen key: {self.zen_key_source}")
        return " · ".join(parts) if parts else "no provider configured yet"


def opencode_auth_summary() -> OpenCodeAuthSummary:
    """Summarize the local OpenCode credential state for setup display."""
    zen = resolve_opencode_zen_key()
    return OpenCodeAuthSummary(
        installed=harness_cli_installed(OPENCODE_KEY),
        stored_providers=_stored_providers(),
        env_providers=_env_providers(),
        zen_key_source=zen[0] if zen is not None else None,
    )
