"""Detect a usable Cline credential for the ``cline-native`` harness.

The ``cline`` CLI owns its own auth: ``cline auth`` runs an interactive
provider picker (device-flow OAuth for hosted providers like ``cline-pass``,
or API-key entry for BYO providers) and persists the result in
``~/.cline/data/settings/providers.json`` — the same file the hub harness
reads provider/model selection from.

Detection is subprocess-free and reads only local state: a
``lastUsedProvider`` (or any configured provider) whose settings carry
credentials (``auth`` tokens or an API key). It cannot detect server-side
revocation — its job is to distinguish a configured cline from an
installed-but-unconfigured one so the setup overview renders the right
status without spawning the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.cline_native.serve import resolve_cline_provider_settings

_CREDENTIAL_KEYS = ("auth", "apiKey", "api_key")


def _providers_path() -> Path:
    """Return the path to Cline's ``providers.json`` settings file."""
    return Path.home() / ".cline" / "data" / "settings" / "providers.json"


def cline_auth_configured(cline_home: Path | None = None) -> bool:
    """Return whether Cline has a usable provider credential on disk.

    :param cline_home: Override for ``~/.cline`` (tests); ``None`` uses home.
    """
    provider, _model = resolve_cline_provider_settings(cline_home)
    if not provider:
        return False
    path = (cline_home or Path.home() / ".cline") / "data" / "settings" / "providers.json"
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    entry = (record.get("providers") or {}).get(provider)
    if not isinstance(entry, dict):
        return False
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        return False
    return any(settings.get(key) for key in _CREDENTIAL_KEYS)


__all__ = ["cline_auth_configured"]
