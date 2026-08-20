"""Detect a Kimi Code (``kimi``) login for the ``kimi`` harness.

The ``kimi`` CLI authenticates against Moonshot AI's backend via ``kimi login``
(an interactive OAuth flow) or by using a Moonshot API key configured in
``$KIMI_CODE_HOME/config.toml``. It has **no** ``kimi logout`` subcommand and no
first-class "am I logged in?" exit-code probe, so login state is inspected from
local configuration instead (verified live against kimi CLI v0.29.1):

- ``kimi login`` writes ``$KIMI_CODE_HOME/credentials/kimi-code.json`` — the file
  is present exactly when an interactive login has completed.
- API-platform users can configure a Kimi provider with an ``api_key`` or set
  ``KIMI_API_KEY`` in the provider's ``env`` table. When such a provider exists,
  ``kimi`` can authenticate without an interactive login.

Detection is file-based and subprocess-free. Like
:func:`omnigent.onboarding.gemini_auth.gemini_login_detected`, it cannot detect
server-side revocation — its only job is to reject the "no usable credential"
case so the readiness layer can distinguish a configured kimi from an
installed-but-unconfigured one without spawning a subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

import tomllib

from omnigent.kimi_native_credentials import resolve_user_kimi_home

# Where ``kimi login`` writes its credential after a completed sign-in
# (verified against kimi CLI v0.29.1). Present and non-empty exactly when an
# interactive login has completed.
KIMI_CREDENTIALS_PATH: Path = (
    Path(os.path.expanduser("~")) / ".kimi-code" / "credentials" / "kimi-code.json"
)

# Kimi provider type as written in Kimi Code's config.toml.
_KIMI_PROVIDER_TYPE = "kimi"
# Conventional env var name for a Kimi API key inside [providers.<name>.env].
_KIMI_API_KEY_ENV_VAR = "KIMI_API_KEY"


def _kimi_config_path() -> Path:
    """Return the path to the user's Kimi Code ``config.toml``.

    Mirrors ``resolve_user_kimi_home()`` (``$KIMI_CODE_HOME`` when set, else
    ``~/.kimi-code``) and appends ``config.toml``.
    """
    return resolve_user_kimi_home() / "config.toml"


def _has_kimi_api_key(config: dict[str, object]) -> bool:
    """Return whether *config* contains a usable Kimi API key provider.

    A usable provider has ``type = "kimi"`` and either a non-empty ``api_key``
    field or a non-empty ``KIMI_API_KEY`` entry in its ``env`` sub-table.
    """
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        if provider.get("type") != _KIMI_PROVIDER_TYPE:
            continue
        api_key = provider.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return True
        env = provider.get("env")
        if isinstance(env, dict):
            env_key = env.get(_KIMI_API_KEY_ENV_VAR)
            if isinstance(env_key, str) and env_key.strip():
                return True
    return False


def kimi_api_key_configured(config_path: Path | None = None) -> bool:
    """Return whether a Kimi API key is configured in ``config.toml``.

    Reads the user's Kimi Code config (respecting ``$KIMI_CODE_HOME``) and
    checks for at least one provider of type ``kimi`` with a non-empty
    ``api_key`` or ``env.KIMI_API_KEY``.

    :param config_path: A specific config file to check; ``None`` uses
        ``$KIMI_CODE_HOME/config.toml``.
    :returns: ``True`` when a Kimi API key provider is configured;
        ``False`` when the file is missing, malformed, or has no usable key.
    """
    path = config_path if config_path is not None else _kimi_config_path()
    try:
        with path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # Missing, unreadable, or broken TOML is treated as "no API key" rather
        # than crashing the readiness refresh.
        return False
    return _has_kimi_api_key(config)


def kimi_login_detected(creds_path: Path | None = None) -> bool:
    """Return whether ``kimi`` has a completed interactive login on this machine.

    Subprocess-free file check: ``kimi login`` writes its credential to
    :data:`KIMI_CREDENTIALS_PATH`, so a present, non-empty file is treated as a
    completed sign-in. This cannot detect server-side revocation — it only
    rejects the "no-credential / file-empty" case. Used by the readiness layer
    to distinguish a signed-in kimi from an installed-but-unconfigured one.

    :param creds_path: A specific credential file to check; ``None`` uses
        :data:`KIMI_CREDENTIALS_PATH`.
    :returns: ``True`` when the credential file exists and is non-empty;
        ``False`` when it is missing, empty, or unreadable.
    """
    path = creds_path if creds_path is not None else KIMI_CREDENTIALS_PATH
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        # A stat/permission error on the credential path is treated as "no
        # usable credential" rather than crashing the readiness refresh.
        return False


def kimi_auth_configured(
    *,
    creds_path: Path | None = None,
    config_path: Path | None = None,
) -> bool:
    """Return whether ``kimi`` has any usable credential on this machine.

    Combines the interactive-login credential check with the API-key config
    check. Either a non-empty ``kimi-code.json`` from ``kimi login`` or a
    configured Kimi API key provider counts as configured.

    :param creds_path: A specific credential file to check; ``None`` uses
        :data:`KIMI_CREDENTIALS_PATH`.
    :param config_path: A specific config file to check; ``None`` uses
        ``$KIMI_CODE_HOME/config.toml``.
    :returns: ``True`` when an interactive-login credential exists or a Kimi
        API key is configured; ``False`` when neither is present.
    """
    return kimi_login_detected(creds_path) or kimi_api_key_configured(config_path)
