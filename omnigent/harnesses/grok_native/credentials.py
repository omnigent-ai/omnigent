"""Command Code-style binary resolution + auth guidance for Grok Build.

The grok_native harness is own-auth: it launches the vendor ``grok`` CLI,
which reads ``~/.grok/auth.json`` (``grok login`` / ``grok login
--device-auth``) or ``XAI_API_KEY``. Omnigent stores no credential.

Executable resolution order matches the other native harnesses:
``OMNIGENT_GROK_PATH`` env override, then ``grok`` on ``PATH``.
"""

from __future__ import annotations

import os
import shutil

_ENV_PATH = "OMNIGENT_GROK_PATH"
_BINARY_CANDIDATES = ("grok",)

INSTALL_HINT = "curl -fsSL https://x.ai/cli/install.sh | bash"
LOGIN_HINT = "grok login --device-auth"
API_KEY_HINT = "export XAI_API_KEY=..."


def resolve_grok_executable() -> str | None:
    """Return the ``grok`` binary path, honoring ``OMNIGENT_GROK_PATH``."""
    override = os.environ.get(_ENV_PATH)
    if override:
        return override if os.path.isfile(override) else None
    for name in _BINARY_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def missing_grok_message() -> str:
    """Guidance shown when no ``grok`` binary resolves."""
    return (
        "Grok Build CLI not found. Install it with:\n"
        f"  {INSTALL_HINT}\n"
        f"Then sign in: `{LOGIN_HINT}` (xAI OAuth), or {API_KEY_HINT}."
    )


def missing_grok_auth_message() -> str:
    """Guidance shown when the CLI exists but no credential is on disk/env."""
    return (
        "Grok Build is not signed in. Run:\n"
        f"  {LOGIN_HINT}\n"
        f"or set XAI_API_KEY in the environment that runs Omnigent."
    )


__all__ = [
    "API_KEY_HINT",
    "INSTALL_HINT",
    "LOGIN_HINT",
    "missing_grok_auth_message",
    "missing_grok_message",
    "resolve_grok_executable",
]
