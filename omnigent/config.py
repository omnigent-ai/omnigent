"""Read Omnigent's user and project configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_GLOBAL_CONFIG_PATH = Path.home() / ".omnigent" / "config.yaml"
_LOCAL_CONFIG_RELPATH = Path(".omnigent") / "config.yaml"


def global_config_path(default_path: Path | None = None) -> Path:
    """Return the effective user-level config path."""
    if config_home := os.environ.get(_CONFIG_HOME_ENV_VAR):
        return Path(config_home) / "config.yaml"
    return default_path or _GLOBAL_CONFIG_PATH


def load_global_config(path: Path | None = None) -> dict[str, Any]:  # type: ignore[explicit-any]
    """Load the user-level config, returning an empty mapping when absent."""
    resolved_path = path or global_config_path()
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: dict[str, Any] = yaml.safe_load(config_file) or {}  # type: ignore[explicit-any]
        return raw


def load_local_config(path: Path | None = None) -> dict[str, Any]:  # type: ignore[explicit-any]
    """Load the project-level config, returning an empty mapping when absent."""
    resolved_path = path or Path.cwd() / _LOCAL_CONFIG_RELPATH
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: dict[str, Any] = yaml.safe_load(config_file) or {}  # type: ignore[explicit-any]
        return raw


def _merge_effective_config(
    global_cfg: dict[str, Any],  # type: ignore[explicit-any]
    local_cfg: dict[str, Any],  # type: ignore[explicit-any]
) -> dict[str, Any]:  # type: ignore[explicit-any]
    """Merge global+local config, deep-merging the ``harness`` mapping.

    A flat ``{**global, **local}`` would make a local ``harness`` mapping
    replace the global one entirely, dropping the user's global
    per-harness overrides. So the ``harness`` key is merged one level deep
    (per-harness sub-keys, local winning per-field) while every other key
    stays a shallow replace (local wins outright). See
    :mod:`omnigent.harness_startup_config` for the ``harness:`` shape.

    :param global_cfg: User-level config (``~/.omnigent/config.yaml``).
    :param local_cfg: Project-level config (``.omnigent/config.yaml``).
    :returns: The merged effective config dict.
    """
    merged: dict[str, Any] = {**global_cfg, **local_cfg}  # type: ignore[explicit-any]
    g_harness = global_cfg.get("harness")
    l_harness = local_cfg.get("harness")
    # Only deep-merge when BOTH are mappings. A scalar on either side is
    # an explicit whole-value override (legacy scalar form, or a project
    # that intentionally pins the whole harness key), so the shallow
    # ``{**global, **local}`` result already in ``merged`` is correct.
    if isinstance(g_harness, dict) and isinstance(l_harness, dict):
        combined: dict[str, Any] = {**g_harness, **l_harness}  # type: ignore[explicit-any]
        # Per-harness sub-keys (anything but ``default``): merge one level
        # deep so a local per-harness entry augments rather than replaces
        # the global one (local fields win per-field).
        for key in set(g_harness) | set(l_harness):
            if key == "default":
                continue
            g_entry = g_harness.get(key)
            l_entry = l_harness.get(key)
            if isinstance(g_entry, dict) and isinstance(l_entry, dict):
                combined[key] = {**g_entry, **l_entry}
        merged["harness"] = combined
    return merged


def load_effective_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """Merge user and project config, with project values taking precedence.

    The ``harness`` mapping is deep-merged (per-harness sub-keys, local
    winning per-field) so a project's per-harness overrides augment —
    rather than replace — the user's global ones. Every other key is a
    shallow replace.
    """
    return _merge_effective_config(load_global_config(), load_local_config())


__all__ = [
    "global_config_path",
    "load_effective_config",
    "load_global_config",
    "load_local_config",
]
