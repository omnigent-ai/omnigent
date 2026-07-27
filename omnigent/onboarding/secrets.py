"""A small secret store for OS-keychain-backed provider credentials.

This is the storage layer behind ``keychain:<name>`` secret references in
``~/.omnigent/config.yaml`` (see
:func:`omnigent.onboarding.provider_config.resolve_secret`). The
``omnigent setup --no-internal-beta`` command writes a provider's API key here
under a stable name (e.g. ``"anthropic"``), and the runtime reads it back
when a family's ``api_key_ref`` is ``keychain:anthropic``.

Two backends, picked transparently:

- **OS keychain** (macOS Keychain, GNOME Keyring, Windows Credential
  Locker) via the ``keyring`` package (a dependency). Used when a backend
  actually works and ``OMNIGENT_DISABLE_KEYRING`` is not set. Secrets
  live in the OS credential store keyed by the service name
  :data:`_KEYRING_SERVICE`.
- **A ``0600`` JSON file** at ``<config_home>/secrets.json`` (a flat
  ``{name: secret}`` mapping). Used when the user forces it via
  ``OMNIGENT_DISABLE_KEYRING`` or a keyring call raises
  :class:`keyring.errors.KeyringError` (e.g. a headless box with no
  unlocked keyring backend). The file is created with — and re-chmod'd
  to — mode ``0600`` so other users on a shared host cannot read it.

``keyring`` is a dependency, but it has no usable backend on every host
(headless Linux without a Secret Service, locked keyrings, CI); the file
backend is the complete, self-contained fallback for those cases.

This module must not import
:mod:`omnigent.onboarding.provider_config` — that module imports *this*
one lazily inside :func:`~omnigent.onboarding.provider_config.resolve_secret`
to avoid a circular import.
"""

from __future__ import annotations

import json
import os

import keyring
import keyring.errors

# The subset of keyring exceptions that mean "this backend can't serve the
# request" (locked / headless / no backend) — we fall back to the file
# backend rather than crash.
_KEYRING_ERRORS: tuple[type[Exception], ...] = (keyring.errors.KeyringError,)

# Service name under which secrets are stored in the OS keychain. A single
# service groups all omnigent secrets; the per-secret ``name`` is the
# keychain "username".
_KEYRING_SERVICE = "omnigent"

# Env var that forces the file backend even when ``keyring`` is importable.
# Useful on CI / headless hosts where an OS keyring exists but is locked.
_DISABLE_KEYRING_ENV = "OMNIGENT_DISABLE_KEYRING"

# Backend identifiers returned by :func:`active_backend`.
KEYRING_BACKEND = "keyring"
FILE_BACKEND = "file"


def _keyring_disabled() -> bool:
    """Return whether the file backend is forced via the environment.

    Mirrors the repo's truthy-env convention (see
    :func:`omnigent.runtime.telemetry._env_bool`): ``"true"`` / ``"1"`` /
    ``"yes"`` (case-insensitive) count as set; anything else (including
    unset) does not.

    :returns: ``True`` when ``OMNIGENT_DISABLE_KEYRING`` is truthy, e.g.
        with ``OMNIGENT_DISABLE_KEYRING=1`` set.
    """
    return os.environ.get(_DISABLE_KEYRING_ENV, "").strip().lower() in ("true", "1", "yes")


def _use_keyring() -> bool:
    """Return whether the OS-keychain backend should be used.

    The keychain backend is used unless the user forces the file backend
    via :data:`_DISABLE_KEYRING_ENV`. A failure *inside* a keyring call (a
    :class:`keyring.errors.KeyringError`, e.g. a locked or headless
    backend) is handled at the call site by falling back to the file
    backend, not here.

    :returns: ``True`` unless ``OMNIGENT_DISABLE_KEYRING`` is truthy.
    """
    return not _keyring_disabled()


def active_backend() -> str:
    """Return the secret backend currently in effect, for diagnostics.

    Used by the readout / setup command to tell the user where their
    secrets are stored. This reflects the *configured* preference (keyring
    when importable and not disabled), not whether a specific keyring call
    would succeed — a transient keyring failure falls back to the file
    backend at the call site.

    :returns: :data:`KEYRING_BACKEND` (``"keyring"``) or
        :data:`FILE_BACKEND` (``"file"``).
    """
    return KEYRING_BACKEND if _use_keyring() else FILE_BACKEND


def _config_home() -> str:
    """Return the omnigent config home directory.

    Respects ``$OMNIGENT_CONFIG_HOME`` for test isolation, matching the
    convention in :func:`omnigent.onboarding.provider_config._config_path`.

    :returns: The config home path, e.g. ``"/home/u/.omnigent"`` or the
        value of ``$OMNIGENT_CONFIG_HOME`` when set.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    if config_home:
        return config_home
    return os.path.join(os.path.expanduser("~"), ".omnigent")


def _secrets_path() -> str:
    """Return the path to the file-backend secrets file.

    :returns: Path to ``secrets.json`` under the config home, e.g.
        ``"/home/u/.omnigent/secrets.json"``.
    """
    return os.path.join(_config_home(), "secrets.json")


def _read_secrets_file() -> dict[str, str]:
    """Read the file-backend secrets mapping.

    :returns: The ``{name: secret}`` mapping, e.g.
        ``{"anthropic": "sk-ant-..."}``. Empty when the file does not
        exist yet.
    :raises json.JSONDecodeError: If the file exists but is not valid JSON
        (we fail loud rather than silently discarding stored secrets).
    """
    path = _secrets_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data: dict[str, str] = json.load(f)
    return data


def _write_secrets_file(secrets: dict[str, str]) -> None:
    """Write the file-backend secrets mapping with ``0600`` permissions.

    Creates the config home directory if absent, writes the JSON mapping,
    and (re-)applies mode ``0600`` to the file so that on a shared host no
    other user can read stored API keys.

    :param secrets: The full ``{name: secret}`` mapping to persist, e.g.
        ``{"anthropic": "sk-ant-...", "openrouter": "sk-or-..."}``.
    """
    path = _secrets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Create the file 0600 from the start via os.open(O_CREAT, 0o600) rather than
    # a plain open()+chmod-after: the latter briefly leaves a freshly-created
    # secrets file group/world-readable until the chmod lands. The trailing chmod
    # still tightens an already-existing file a prior run may have created wider.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(secrets, f, indent=2)
    os.chmod(path, 0o600)


def store_secret(name: str, value: str) -> None:
    """Store *value* under *name*, using the active backend.

    Tries the OS keychain when enabled; on a :class:`keyring.errors.KeyringError`
    (e.g. a locked or absent keyring) it transparently falls back to the
    ``0600`` JSON file so the secret is never lost to a keyring hiccup.

    :param name: The stable secret name, matching the ``<name>`` in a
        ``keychain:<name>`` reference, e.g. ``"anthropic"``.
    :param value: The secret value to store, e.g. ``"sk-ant-..."``.
    """
    if _use_keyring():
        try:
            keyring.set_password(_KEYRING_SERVICE, name, value)
            return
        except _KEYRING_ERRORS:
            # Keyring is importable but the backend can't store right now
            # (locked, headless, no backend) — fall through to the file.
            pass
    secrets = _read_secrets_file()
    secrets[name] = value
    _write_secrets_file(secrets)


def load_secret(name: str) -> str | None:
    """Return the secret stored under *name*, or ``None`` if absent.

    Tries the OS keychain when enabled; on a :class:`keyring.errors.KeyringError`
    it falls back to the file backend. A missing secret (in either backend)
    returns ``None`` so callers can fail loud with a name-specific message.

    :param name: The stable secret name, e.g. ``"anthropic"``.
    :returns: The stored secret value, e.g. ``"sk-ant-..."``, or ``None``
        when no secret is stored under *name*.
    """
    if _use_keyring():
        try:
            stored: str | None = keyring.get_password(_KEYRING_SERVICE, name)
            return stored
        except _KEYRING_ERRORS:
            pass
    return _read_secrets_file().get(name)


def delete_secret(name: str) -> None:
    """Delete the secret stored under *name*, if present.

    Tries the OS keychain when enabled; on a :class:`keyring.errors.KeyringError`
    (which includes ``PasswordDeleteError`` for an absent entry) it falls
    back to the file backend. Deleting a name that does not exist is a
    no-op in either backend.

    :param name: The stable secret name to delete, e.g. ``"anthropic"``.
    """
    if _use_keyring():
        try:
            keyring.delete_password(_KEYRING_SERVICE, name)
            return
        except _KEYRING_ERRORS:
            pass
    secrets = _read_secrets_file()
    if name in secrets:
        del secrets[name]
        _write_secrets_file(secrets)
