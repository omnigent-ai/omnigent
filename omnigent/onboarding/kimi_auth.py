"""Detect a usable Kimi Code (``kimi``) credential for the ``kimi`` harness.

The ``kimi`` CLI authenticates against Moonshot AI's backend two ways:

- ``kimi login`` runs an interactive OAuth device flow. This grants
  **membership** benefits and writes ``$KIMI_CODE_HOME/credentials/kimi-code.json``
  (default ``~/.kimi-code/credentials/kimi-code.json``) — present exactly when
  an interactive login has completed.
- **Pay-per-use / API-platform** users cannot use ``kimi login``: the Kimi Code
  coding endpoint rejects OAuth credentials without an active membership. They
  authenticate instead by configuring a Kimi provider with an ``api_key`` (or an
  ``env.KIMI_API_KEY``) in ``$KIMI_CODE_HOME/config.toml``. When such a provider
  exists, ``kimi`` authenticates without an interactive login.

Detection is subprocess-free and reads only local state (verified live against
kimi CLI v0.29.1): a non-empty credential file, or a Kimi provider with an API
key in the config. Like
:func:`omnigent.onboarding.gemini_auth.gemini_login_detected`, it cannot detect
server-side revocation — its only job is to reject the "no usable credential"
case so the readiness layer can distinguish a configured kimi from an
installed-but-unconfigured one without spawning a subprocess.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import tomllib

from omnigent.kimi_native_credentials import resolve_user_kimi_home

# Provider ``type`` values that identify a Kimi / Moonshot API-key provider in
# ``config.toml``. The manual coding-endpoint block uses ``kimi``; a provider
# imported via ``kimi provider catalog add moonshotai`` carries the catalog's
# own type (``moonshotai`` / its China variant, or ``moonshot``). Match all of
# them so both pay-per-use paths are recognized.
_KIMI_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"kimi", "moonshot", "moonshotai", "moonshotai-cn"}
)
# Endpoint hosts that identify a Kimi / Moonshot provider when its ``type`` is
# opaque (an unrecognized catalog spelling): the Moonshot open platform
# (``api.moonshot.ai`` / China ``api.moonshot.cn``) and the Kimi Code coding
# endpoint (``api.kimi.com``).
_KIMI_PROVIDER_HOSTS: tuple[str, ...] = ("moonshot.ai", "moonshot.cn", "kimi.com")
# Env var names a Kimi / Moonshot API key is conventionally set under inside a
# provider's ``[providers.<name>.env]`` table.
_KIMI_API_KEY_ENV_VARS: tuple[str, ...] = ("KIMI_API_KEY", "MOONSHOT_API_KEY")


def _kimi_config_path() -> Path:
    """Return the path to the user's Kimi Code ``config.toml``.

    Mirrors :func:`~omnigent.kimi_native_credentials.resolve_user_kimi_home`
    (``$KIMI_CODE_HOME`` when set, else ``~/.kimi-code``) and appends
    ``config.toml``.
    """
    return resolve_user_kimi_home() / "config.toml"


def _kimi_credentials_path() -> Path:
    """Return the credential file ``kimi login`` writes on a completed sign-in.

    ``$KIMI_CODE_HOME/credentials/kimi-code.json`` (default
    ``~/.kimi-code/credentials/kimi-code.json``), resolved at call time via
    :func:`~omnigent.kimi_native_credentials.resolve_user_kimi_home` so it
    honors ``$KIMI_CODE_HOME`` — matching where kimi actually stores auth.
    """
    return resolve_user_kimi_home() / "credentials" / "kimi-code.json"


def _host_is_kimi(base_url: object) -> bool:
    """Return whether *base_url* points at a Kimi / Moonshot endpoint host."""
    if not isinstance(base_url, str) or not base_url.strip():
        return False
    # Parse a scheme-relative form when the value omits a scheme, so a bare
    # ``api.moonshot.ai/v1`` still resolves a hostname instead of ``None``.
    value = base_url.strip()
    if "://" not in value:
        value = f"//{value}"
    host = (urlparse(value).hostname or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in _KIMI_PROVIDER_HOSTS)


def _provider_is_kimi(provider: dict[str, object]) -> bool:
    """Return whether *provider* is a Kimi / Moonshot provider.

    Identified by a known ``type`` (covers the manual ``kimi`` block and the
    ``moonshotai`` catalog import) or, when the type is unrecognized, by a
    ``base_url`` on a Moonshot / Kimi host. This keeps an unrelated provider
    (e.g. an OpenAI entry the user added to kimi) from counting as Kimi auth.
    """
    provider_type = provider.get("type")
    if isinstance(provider_type, str) and provider_type.lower() in _KIMI_PROVIDER_TYPES:
        return True
    return _host_is_kimi(provider.get("base_url"))


def _provider_has_key(provider: dict[str, object]) -> bool:
    """Return whether *provider* carries a non-empty API key.

    Accepts an inline ``api_key`` or a non-empty ``KIMI_API_KEY`` /
    ``MOONSHOT_API_KEY`` entry in the provider's ``env`` sub-table — the two
    shapes ``config.toml`` uses for a credential.
    """
    api_key = provider.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return True
    env = provider.get("env")
    if isinstance(env, dict):
        for var in _KIMI_API_KEY_ENV_VARS:
            value = env.get(var)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _default_provider_name(config: dict[str, object]) -> str | None:
    """Return the provider name ``default_model`` selects, if any.

    Kimi Code's ``default_model`` is spelled ``"<provider>/<model...>"``
    (e.g. ``"openrouter/moonshotai/kimi-k3"``); the segment before the first
    ``/`` names the provider table kimi routes to by default.
    """
    default_model = config.get("default_model")
    if not isinstance(default_model, str):
        return None
    provider, sep, _model = default_model.strip().partition("/")
    if not sep or not provider:
        return None
    return provider


def _has_kimi_api_key(config: dict[str, object]) -> bool:
    """Return whether *config* declares a usable API-key provider for kimi.

    A usable provider is one carrying a non-empty API key that kimi will
    actually authenticate with: a Kimi / Moonshot provider (by ``type`` or
    endpoint host — both pay-per-use paths), or the provider ``default_model``
    routes to — Kimi Code supports custom OpenAI-compatible providers (e.g.
    OpenRouter), and one selected as the default makes kimi fully usable
    without any Moonshot credential. An unrelated provider that is neither
    Kimi nor the configured default still does not count.
    """
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False
    default_provider = _default_provider_name(config)
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        if not _provider_has_key(provider):
            continue
        if _provider_is_kimi(provider) or name == default_provider:
            return True
    return False


def kimi_api_key_configured(config_path: Path | None = None) -> bool:
    """Return whether a Kimi API key is configured in ``config.toml``.

    Reads the user's Kimi Code config (respecting ``$KIMI_CODE_HOME``) and
    checks for at least one provider carrying a non-empty API key that kimi
    authenticates with: a Kimi / Moonshot provider (by ``type`` or endpoint
    host — the Moonshot open platform or the Kimi Code coding endpoint), or a
    custom OpenAI-compatible provider (e.g. OpenRouter) selected by
    ``default_model``. None of these require an interactive ``kimi login``.

    :param config_path: A specific config file to check; ``None`` uses
        ``$KIMI_CODE_HOME/config.toml``.
    :returns: ``True`` when a Kimi API-key provider is configured; ``False``
        when the file is missing, malformed, or has no usable key.
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
    :func:`_kimi_credentials_path` (honoring ``$KIMI_CODE_HOME``), so a present,
    non-empty file is treated as a completed OAuth sign-in. This cannot detect
    server-side revocation — it only rejects the "no-credential / file-empty"
    case.

    :param creds_path: A specific credential file to check; ``None`` uses
        ``$KIMI_CODE_HOME/credentials/kimi-code.json``.
    :returns: ``True`` when the credential file exists and is non-empty;
        ``False`` when it is missing, empty, or unreadable.
    """
    path = creds_path if creds_path is not None else _kimi_credentials_path()
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
    check: either a non-empty ``kimi-code.json`` from ``kimi login`` (membership
    OAuth) or a configured Kimi API-key provider (pay-per-use) counts as
    configured. Used by the readiness layer so API-platform users are not
    wrongly reported as unconfigured just because they never ran ``kimi login``.

    :param creds_path: A specific credential file to check; ``None`` uses
        ``$KIMI_CODE_HOME/credentials/kimi-code.json``.
    :param config_path: A specific config file to check; ``None`` uses
        ``$KIMI_CODE_HOME/config.toml``.
    :returns: ``True`` when an interactive-login credential exists or a Kimi
        API key is configured; ``False`` when neither is present.
    """
    return kimi_login_detected(creds_path) or kimi_api_key_configured(config_path)
