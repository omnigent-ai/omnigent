"""Cursor API-key credential storage for ``omnigent setup`` and the runtime.

Cursor is deliberately outside the anthropic/openai provider-family + gateway
machinery (see :func:`omnigent.runtime.workflow._build_cursor_spawn_env`): the
Cursor SDK (``cursor-sdk``) talks only to Cursor's own backend via a
``CURSOR_API_KEY`` — which it requires — never the Databricks AI gateway. It
therefore has no ``providers:`` family entry, but a user should still be able to
register a ``CURSOR_API_KEY`` once through ``omnigent setup`` rather than
exporting it in every shell.

This module is that home. The key is stored exactly like the api-key
providers' secrets — in the omnigent secret store (OS keychain, else a
``0600`` JSON file; see :mod:`omnigent.onboarding.secrets`) — and referenced
from a dedicated top-level ``cursor:`` block in ``~/.omnigent/config.yaml``::

    cursor:
      api_key_ref: keychain:cursor   # or env:CURSOR_API_KEY

The reference is resolved with the same :func:`resolve_secret` resolver the
provider families use. A dedicated block (rather than the shared global
``auth:`` block) is required because ``auth:`` is the *gateway* credential the
SDK harnesses inherit when their spec declares no auth
(:func:`omnigent.runtime.workflow._load_global_auth`) — a Cursor key parked
there would be mis-consumed by claude-sdk / codex / pi / openai-agents.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import load_config, resolve_secret

# The secret-store name (and thus ``keychain:<name>``) under which a Cursor
# API key is stored — stable so the setup flow and the resolver agree.
CURSOR_SECRET_NAME = "cursor"

# The OPTIONAL pip extra that ships the Cursor SDK (``cursor-sdk``) — not in
# the default install, so the ``cursor:`` key can be set with no SDK present.
# Setup surfaces the command verbatim when the extra is missing. Mirrors
# antigravity's ``ANTIGRAVITY_EXTRA`` / ``ANTIGRAVITY_EXTRA_INSTALL_COMMAND``.
# The name carries literal brackets — markup-rendered surfaces must escape it.
CURSOR_EXTRA = "cursor"
CURSOR_EXTRA_INSTALL_COMMAND = 'pip install "omnigent[cursor]"'


def cursor_sdk_installed() -> bool:
    """Return whether the ``cursor-sdk`` SDK (the optional extra) is importable.

    The executor imports it lazily on the first turn
    (:mod:`omnigent.inner.cursor_executor`), so a key can be set with no SDK;
    setup uses this to detect that and offer to install it. Mirrors
    :func:`omnigent.onboarding.databricks_config.databricks_sdk_installed` /
    :func:`omnigent.onboarding.antigravity_auth.antigravity_sdk_installed`:
    :func:`importlib.util.find_spec` avoids importing the heavy SDK, and the
    guard catches the ``ModuleNotFoundError`` it raises when a parent package is
    absent.

    :returns: ``True`` when ``cursor_sdk`` is importable.
    """
    try:
        return importlib.util.find_spec("cursor_sdk") is not None
    except ModuleNotFoundError:
        # Guard like the antigravity/databricks checks: find_spec can raise
        # (not return None) when a parent package is absent.
        return False


def cursor_install_command() -> list[str]:
    """Return the argv that installs the ``cursor`` extra into this env.

    Prefers ``uv pip install`` when ``uv`` is on ``PATH``, else this
    interpreter's own pip (``sys.executable -m pip``) so the package lands in
    the running install. Carries **no index URL** — pip/uv pick up the user's
    configured index, so a private proxy is honored without hardcoding one.
    Mirrors :func:`omnigent.onboarding.antigravity_auth.antigravity_install_command`.

    :returns: The install argv, e.g.
        ``["uv", "pip", "install", "omnigent[cursor]"]`` or
        ``[sys.executable, "-m", "pip", "install", "omnigent[cursor]"]``.
    """
    target = f"omnigent[{CURSOR_EXTRA}]"
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install", target]
    return [sys.executable, "-m", "pip", "install", target]


def install_cursor_sdk() -> bool:
    """Install the ``cursor`` extra; return whether the SDK is now present.

    Shells out to :func:`cursor_install_command` and re-checks
    :func:`cursor_sdk_installed`; pip/uv output is not captured so failures are
    visible. Mirrors
    :func:`omnigent.onboarding.antigravity_auth.install_antigravity_sdk`.

    :returns: ``True`` when ``cursor_sdk`` is importable after the attempt;
        ``False`` if the process failed to spawn, timed out, or the SDK is still
        absent.
    """
    try:
        subprocess.run(cursor_install_command(), check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Invalidate import caches so a just-installed package is seen without
    # restarting the process.
    importlib.invalidate_caches()
    return cursor_sdk_installed()


# The dedicated top-level config block and the field that references the key.
CURSOR_CONFIG_KEY = "cursor"
_API_KEY_REF_FIELD = "api_key_ref"
_API_KEY_FIELD = "api_key"

# Cursor API keys are issued with this prefix (e.g. ``crsr_AbC123…``); the
# setup flow validates against it so an obviously-wrong paste (a different
# vendor's key, a stray token) is caught before it is stored. The check is
# deliberately *soft* — a user may force a non-matching value through — so a
# future prefix change can never lock anyone out of their own key.
CURSOR_API_KEY_PREFIX = "crsr_"


def looks_like_cursor_api_key(value: str) -> bool:
    """Return whether *value* has the shape of a Cursor API key.

    :param value: A pasted/typed candidate key, e.g. ``"crsr_AbC123"``.
    :returns: ``True`` when *value* starts with :data:`CURSOR_API_KEY_PREFIX`.
    """
    return value.startswith(CURSOR_API_KEY_PREFIX)


def cursor_api_key_ref(config: dict[str, object] | None = None) -> str | None:
    """Return the configured Cursor API-key secret reference, if any.

    Reads the dedicated ``cursor:`` block of the global config. Both the
    ``api_key_ref`` (``keychain:`` / ``env:``) and an inline ``api_key``
    (``$VAR`` / literal) shapes are accepted so a hand-edited config works
    too; ``api_key_ref`` wins when both are present.

    :param config: A pre-loaded config mapping; ``None`` loads
        ``~/.omnigent/config.yaml`` via :func:`load_config`.
    :returns: The secret reference, e.g. ``"keychain:cursor"`` or
        ``"env:CURSOR_API_KEY"``, or ``None`` when no Cursor key is
        configured.
    """
    cfg = load_config() if config is None else config
    block = cfg.get(CURSOR_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    ref = block.get(_API_KEY_REF_FIELD) or block.get(_API_KEY_FIELD)
    return ref if isinstance(ref, str) and ref else None


def resolve_cursor_api_key(config: dict[str, object] | None = None) -> str | None:
    """Resolve the configured Cursor API key to its plaintext value, softly.

    Looks up the ``cursor:`` block's secret reference and resolves it via
    :func:`resolve_secret`. Unlike :func:`resolve_secret`, this **never
    raises**: a missing block or an unresolvable reference (deleted keychain
    entry, unset env var) returns ``None`` so the caller — the cursor
    spawn-env builder and the setup readout — can fall back to an inherited
    ``CURSOR_API_KEY`` instead of crashing a run.

    An empty / all-whitespace resolved value also reads as ``None``: the
    shared ``resolve_secret`` ``env:`` branch only raises on an *unset*
    variable, so a configured ``env:CURSOR_API_KEY`` pointing at an empty
    (``CURSOR_API_KEY=""``) or whitespace-only var resolves to ``""``. Folding
    that to ``None`` here keeps :func:`cursor_api_key_configured` and the
    spawn-env builder in agreement — both treat such a value as unset rather
    than reporting "key set" for a credential the runtime won't forward.
    (``keychain:`` values are stripped at store time, so only the ``env:``
    path needs this runtime guard; we apply it uniformly for simplicity.)

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: The plaintext Cursor API key, or ``None`` when none is
        configured or it cannot be resolved.
    """
    ref = cursor_api_key_ref(config)
    if ref is None:
        return None
    try:
        resolved = resolve_secret(ref)
    except OmnigentError:
        return None
    return resolved if resolved.strip() else None


def cursor_api_key_configured(config: dict[str, object] | None = None) -> bool:
    """Return whether a usable Cursor API key is configured.

    ``True`` only when the ``cursor:`` block names a reference **and** it
    resolves — a dangling reference reads as not-configured so the setup
    readout never claims a credential the runtime can't actually use.

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: ``True`` when a Cursor API key is configured and resolvable.
    """
    return resolve_cursor_api_key(config) is not None


def cursor_api_key_settings(ref: str) -> dict[str, object]:
    """Build the ``{"cursor": {...}}`` settings dict that records *ref*.

    Handed to :func:`omnigent.cli._save_global_config` (a shallow update, so
    it replaces the whole ``cursor:`` block) to persist the reference.

    :param ref: The secret reference to record, e.g. ``"keychain:cursor"``
        or ``"env:CURSOR_API_KEY"``.
    :returns: ``{"cursor": {"api_key_ref": ref}}``.
    """
    return {CURSOR_CONFIG_KEY: {_API_KEY_REF_FIELD: ref}}
