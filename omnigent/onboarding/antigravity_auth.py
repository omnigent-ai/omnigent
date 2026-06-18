"""Antigravity Gemini API-key credential storage for ``omnigent setup``.

Antigravity is Gemini-native (the SDK has no OpenAI-compatible ``base_url``), so
it sits outside the anthropic/openai provider-family machinery. Its key lives in
the omnigent secret store and is referenced from a dedicated top-level
``antigravity:`` block in ``~/.omnigent/config.yaml`` (``keychain:`` / ``env:``),
resolved with the shared :func:`resolve_secret`. A dedicated block — not the
global ``auth:`` block, which the other SDK harnesses inherit — keeps a Gemini
key from being mis-consumed by claude-sdk / codex / pi / openai-agents. Mirrors
:mod:`omnigent.onboarding.cursor_auth`.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import load_config, resolve_secret

# Stable secret-store name (and thus ``keychain:<name>``) so setup and the
# resolver agree.
ANTIGRAVITY_SECRET_NAME = "antigravity"

# The Gemini-native SDK (``google-antigravity``) ships in an OPTIONAL extra, so a
# user can configure the ``antigravity:`` key in setup and still have no SDK to run
# the harness; setup surfaces this command when the extra is missing. (Cursor needs
# no parallel: ``cursor-sdk`` is a baseline dep.) The literal brackets must be
# escaped on any markup-rendered surface.
ANTIGRAVITY_EXTRA = "antigravity"
ANTIGRAVITY_EXTRA_INSTALL_COMMAND = 'pip install "omnigent[antigravity]"'


def antigravity_sdk_installed() -> bool:
    """Return whether the ``google-antigravity`` SDK (the optional extra) is importable.

    Setup uses this to detect a missing SDK and offer to install it. Mirrors
    :func:`omnigent.onboarding.databricks_config.databricks_sdk_installed`: uses
    :func:`importlib.util.find_spec` to avoid importing the heavy SDK, and guards the
    ``ModuleNotFoundError`` ``find_spec`` raises when the parent ``google`` namespace
    package is absent (it raises instead of returning ``None``).

    :returns: ``True`` when ``google.antigravity`` is importable.
    """
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except ModuleNotFoundError:
        # Raised (not None) when the parent `google` namespace package is absent.
        return False


def antigravity_install_command() -> list[str]:
    """Return the argv that installs the ``antigravity`` extra into this env.

    Prefers ``uv pip install`` when ``uv`` is on ``PATH``, else this interpreter's own
    pip (``sys.executable -m pip``) so the package lands in the running install.
    Deliberately carries **no index URL**: pip/uv pick up the user's own configured
    index, so a private proxy is honored without hardcoding one into committed code.

    :returns: The install argv, e.g.
        ``["uv", "pip", "install", "omnigent[antigravity]"]`` or
        ``[sys.executable, "-m", "pip", "install", "omnigent[antigravity]"]``.
    """
    target = f"omnigent[{ANTIGRAVITY_EXTRA}]"
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install", target]
    return [sys.executable, "-m", "pip", "install", target]


def install_antigravity_sdk() -> bool:
    """Install the ``antigravity`` extra; return whether the SDK is now present.

    Shells out to :func:`antigravity_install_command` and re-checks
    :func:`antigravity_sdk_installed`. Surfaces pip/uv output (no capture) so a failing
    install is visible. Mirrors
    :func:`omnigent.onboarding.harness_install.install_harness_cli`.

    :returns: ``True`` when ``google.antigravity`` is importable after the attempt;
        ``False`` when the install failed to spawn, timed out, or the SDK is still
        absent.
    """
    try:
        subprocess.run(antigravity_install_command(), check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Invalidate import caches so a just-installed package is seen without a restart.
    importlib.invalidate_caches()
    return antigravity_sdk_installed()


# The dedicated top-level config block and the field that references the key.
ANTIGRAVITY_CONFIG_KEY = "antigravity"
_API_KEY_REF_FIELD = "api_key_ref"
_API_KEY_FIELD = "api_key"

# Ambient env vars the SDK reads directly; setup offers to adopt either, and the
# spawn-env builder falls back to them when no key is configured.
ANTIGRAVITY_ENV_VARS: tuple[str, ...] = ("GEMINI_API_KEY", "ANTIGRAVITY_API_KEY")

# Gemini / Google API-key prefixes. Legacy keys start with ``AIza`` (e.g.
# ``AIzaSy…``); newer Google API keys start with ``AQ``. Used for a *soft* paste
# check — a non-matching key may be forced through, so a prefix change never
# locks anyone out.
ANTIGRAVITY_API_KEY_PREFIXES = ("AIza", "AQ")

# Human-readable form of the accepted prefixes, e.g. ``'AIza' or 'AQ'``.
ANTIGRAVITY_API_KEY_PREFIX_HINT = " or ".join(
    f"'{p}'" for p in ANTIGRAVITY_API_KEY_PREFIXES
)


def looks_like_gemini_api_key(value: str) -> bool:
    """Return whether *value* looks like a Gemini / Google API key.

    :param value: A pasted candidate, e.g. ``"AIzaSyAbC123"`` or ``"AQ…"``.
    :returns: ``True`` when it starts with one of
        :data:`ANTIGRAVITY_API_KEY_PREFIXES`.
    """
    return value.startswith(ANTIGRAVITY_API_KEY_PREFIXES)


def antigravity_api_key_ref(config: dict[str, object] | None = None) -> str | None:
    """Return the configured Gemini API-key secret reference, if any.

    Reads the ``antigravity:`` block; both ``api_key_ref`` and an inline
    ``api_key`` shape are accepted (``api_key_ref`` wins) so a hand-edited
    config works too.

    :param config: Pre-loaded config; ``None`` loads the global config.
    :returns: The reference, e.g. ``"keychain:antigravity"`` or
        ``"env:GEMINI_API_KEY"``, else ``None``.
    """
    cfg = load_config() if config is None else config
    block = cfg.get(ANTIGRAVITY_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    ref = block.get(_API_KEY_REF_FIELD) or block.get(_API_KEY_FIELD)
    return ref if isinstance(ref, str) and ref else None


def resolve_antigravity_api_key(config: dict[str, object] | None = None) -> str | None:
    """Resolve the configured Gemini API key to plaintext, softly.

    Never raises: a missing block or unresolvable reference returns ``None`` so
    callers fall back to ambient creds / Vertex instead of crashing a run.

    :param config: Pre-loaded config; ``None`` loads the global config.
    :returns: The plaintext key, else ``None``.
    """
    ref = antigravity_api_key_ref(config)
    if ref is None:
        return None
    try:
        return resolve_secret(ref)
    except OmnigentError:
        return None


def antigravity_api_key_configured(config: dict[str, object] | None = None) -> bool:
    """Return whether a usable Gemini API key is configured.

    ``True`` only when the reference resolves — a dangling reference reads as
    not-configured so the setup readout never overclaims.

    :param config: Pre-loaded config; ``None`` loads the global config.
    :returns: ``True`` when a key is configured and resolvable.
    """
    return resolve_antigravity_api_key(config) is not None


def antigravity_api_key_settings(ref: str) -> dict[str, object]:
    """Build the ``{"antigravity": {"api_key_ref": ref}}`` settings dict.

    :param ref: The reference to record, e.g. ``"keychain:antigravity"``.
    :returns: The settings dict for :func:`omnigent.cli._save_global_config`.
    """
    return {ANTIGRAVITY_CONFIG_KEY: {_API_KEY_REF_FIELD: ref}}
