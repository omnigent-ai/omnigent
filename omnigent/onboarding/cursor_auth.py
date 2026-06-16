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

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import load_config, resolve_secret

# The secret-store name (and thus ``keychain:<name>``) under which a Cursor
# API key is stored — stable so the setup flow and the resolver agree.
CURSOR_SECRET_NAME = "cursor"

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

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: The plaintext Cursor API key, or ``None`` when none is
        configured or it cannot be resolved.
    """
    ref = cursor_api_key_ref(config)
    if ref is None:
        return None
    try:
        return resolve_secret(ref)
    except OmnigentError:
        return None


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
