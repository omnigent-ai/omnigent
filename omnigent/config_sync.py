"""Portable user settings that follow a user across their connected hosts.

``~/.omnigent/config.yaml`` is machine-local. The host daemon keeps the
portable subset of the user-level file in step with the server, which stores
one copy per user and fans changes out to the user's other connected hosts.
Only these keys travel:

- ``model`` and ``opencode_model`` (default model preferences)
- ``auto_open_conversation``
- ``harness.default`` and each ``harness.<id>.args`` startup override

Credentials (``providers``), the host identity (``host``), the server
selection, and anything naming a path on one machine (``default_agent``,
``harness.<id>.command``, ``github.accounts``) stay local.

Protocol: a host sends ``host.config_sync`` on connect, whenever its local
portable subset changes, and periodically. The frame carries the revision the
host last applied, its current portable settings, and whether they changed
locally since that revision. The server adopts a local change from a host that
has synced before (last writer wins) and seeds an empty store from any host; a
never-synced host defers to settings the user already has. Every adopted change
bumps the revision and is pushed as ``host.config_update`` to the user's other
connected hosts; the reporting host always receives the current state back.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeAlias

SyncedSettings: TypeAlias = dict[str, object]

SYNCED_MODEL_KEYS: tuple[str, ...] = ("model", "opencode_model")
SYNCED_FLAG_KEYS: tuple[str, ...] = ("auto_open_conversation",)
# ``harness`` is synced only for its ``default`` and per-harness ``args`` fields.
_SYNCED_HARNESS_FIELDS: frozenset[str] = frozenset({"args"})
MAX_SYNCED_SETTINGS_BYTES = 16 * 1024


def _canonical(harness: str) -> str:
    # Deferred like harness_startup_config: config loading must not pull in harness discovery.
    from omnigent.harness_aliases import canonicalize_harness

    return canonicalize_harness(harness) or harness


def synced_settings(cfg: Mapping[str, object]) -> SyncedSettings:
    """Return the portable subset of a user-level config mapping.

    Malformed values are skipped silently; the CLI's own config commands report
    them, and this runs on every host poll.

    :param cfg: A loaded ``config.yaml`` mapping.
    :returns: The settings that follow the user, e.g.
        ``{"model": "gpt-5", "harness": {"codex-native": {"args": ["--profile", "work"]}}}``.
    """
    settings: SyncedSettings = {}
    for key in SYNCED_MODEL_KEYS:
        value = cfg.get(key)
        if isinstance(value, str) and value:
            settings[key] = value
    for key in SYNCED_FLAG_KEYS:
        value = cfg.get(key)
        if isinstance(value, bool):
            settings[key] = value
    harness = _synced_harness(cfg.get("harness"))
    if harness:
        settings["harness"] = harness
    return settings


def _synced_harness(raw: object) -> dict[str, object]:
    """Extract ``default`` and per-harness ``args`` from a ``harness:`` value."""
    if isinstance(raw, str):
        return {"default": raw} if raw else {}
    if not isinstance(raw, Mapping):
        return {}
    default: str | None = None
    entries: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        if key == "default":
            if isinstance(value, str) and value:
                default = value
            continue
        if not isinstance(value, Mapping):
            continue
        args = value.get("args")
        if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
            entries.setdefault(_canonical(key), {})["args"] = list(args)
    result: dict[str, object] = dict(entries)
    if default is not None:
        result["default"] = default
    return result


def apply_synced_settings(
    cfg: Mapping[str, object], settings: Mapping[str, object]
) -> dict[str, object]:
    """Return *cfg* with its portable subset replaced by *settings*.

    Portable keys absent from *settings* are removed; everything else in
    *cfg*, including ``harness.<id>.command``, is preserved. Per-harness ids
    are written in canonical form so a local alias and a synced id share one
    entry.

    :param cfg: The current user-level config mapping.
    :param settings: Validated synced settings (see :func:`validate_synced_settings`).
    :returns: The merged config mapping to write back.
    """
    merged = dict(cfg)
    for key in (*SYNCED_MODEL_KEYS, *SYNCED_FLAG_KEYS):
        if key in settings:
            merged[key] = settings[key]
        else:
            merged.pop(key, None)
    harness = _apply_harness(cfg.get("harness"), settings.get("harness"))
    if harness:
        merged["harness"] = harness
    else:
        merged.pop("harness", None)
    return merged


def _apply_harness(local: object, synced: object) -> dict[str, object]:
    """Overlay synced ``default``/``args`` on the local per-harness entries."""
    result: dict[str, dict[str, object]] = {}
    if isinstance(local, Mapping):
        for key, value in local.items():
            if not isinstance(key, str) or key == "default" or not isinstance(value, Mapping):
                continue
            entry = result.setdefault(_canonical(key), {})
            entry.update(
                {
                    field: field_value
                    for field, field_value in value.items()
                    if field not in _SYNCED_HARNESS_FIELDS
                }
            )
    default: object | None = None
    if isinstance(synced, Mapping):
        for key, value in synced.items():
            if key == "default":
                default = value
            elif isinstance(key, str) and isinstance(value, Mapping):
                result.setdefault(_canonical(key), {}).update(
                    {
                        field: item
                        for field, item in value.items()
                        if field in _SYNCED_HARNESS_FIELDS
                    }
                )
    harness: dict[str, object] = {key: entry for key, entry in result.items() if entry}
    if default is not None:
        harness["default"] = default
    return harness


def validate_synced_settings(value: object) -> SyncedSettings:
    """Check settings received from another host or read from storage.

    :param value: The decoded JSON value.
    :returns: A copy containing only the portable keys with their expected types.
    :raises ValueError: If *value* has an unexpected key, type, or size.
    """
    if not isinstance(value, Mapping):
        raise ValueError("synced settings must be a JSON object")
    if len(json.dumps(value, default=str).encode("utf-8")) > MAX_SYNCED_SETTINGS_BYTES:
        raise ValueError(f"synced settings exceed {MAX_SYNCED_SETTINGS_BYTES} bytes")
    result: SyncedSettings = {}
    for key, item in value.items():
        if key in SYNCED_MODEL_KEYS:
            if not isinstance(item, str) or not item:
                raise ValueError(f"synced setting {key!r} must be a non-empty string")
            result[key] = item
        elif key in SYNCED_FLAG_KEYS:
            if not isinstance(item, bool):
                raise ValueError(f"synced setting {key!r} must be a boolean")
            result[key] = item
        elif key == "harness":
            result[key] = _validate_harness(item)
        else:
            raise ValueError(f"unexpected synced setting {key!r}")
    return result


def _validate_harness(value: object) -> dict[str, object]:
    """Check a ``harness`` mapping and write its per-harness ids in canonical form."""
    if not isinstance(value, Mapping):
        raise ValueError("synced setting 'harness' must be a JSON object")
    default: str | None = None
    entries: dict[str, dict[str, object]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("synced harness ids must be non-empty strings")
        if key == "default":
            if not isinstance(item, str) or not item:
                raise ValueError("synced setting 'harness.default' must be a non-empty string")
            default = item
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"synced setting 'harness.{key}' must be a JSON object")
        entry: dict[str, object] = {}
        for field, field_value in item.items():
            if field not in _SYNCED_HARNESS_FIELDS:
                raise ValueError(f"unexpected synced setting 'harness.{key}.{field}'")
            if not isinstance(field_value, list) or not all(
                isinstance(arg, str) for arg in field_value
            ):
                raise ValueError(f"synced setting 'harness.{key}.args' must be a list of strings")
            entry[field] = list(field_value)
        if not entry:
            raise ValueError(f"synced setting 'harness.{key}' has no portable fields")
        entries.setdefault(_canonical(key), {}).update(entry)
    result: dict[str, object] = dict(entries)
    if default is not None:
        result["default"] = default
    return result


__all__ = [
    "MAX_SYNCED_SETTINGS_BYTES",
    "SyncedSettings",
    "apply_synced_settings",
    "synced_settings",
    "validate_synced_settings",
]
