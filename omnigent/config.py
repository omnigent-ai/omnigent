"""Read Omnigent's user and project configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

import yaml

_Config: TypeAlias = dict[str, object]

_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_GLOBAL_CONFIG_PATH = Path.home() / ".omnigent" / "config.yaml"
_LOCAL_CONFIG_RELPATH = Path(".omnigent") / "config.yaml"


def global_config_path(default_path: Path | None = None) -> Path:
    """Return the effective user-level config path."""
    if config_home := os.environ.get(_CONFIG_HOME_ENV_VAR):
        return Path(config_home) / "config.yaml"
    return default_path or _GLOBAL_CONFIG_PATH


def load_global_config(path: Path | None = None) -> _Config:
    """Load the user-level config, returning an empty mapping when absent."""
    resolved_path = path or global_config_path()
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: _Config = yaml.safe_load(config_file) or {}
        return raw


def load_local_config(path: Path | None = None) -> _Config:
    """Load the project-level config, returning an empty mapping when absent."""
    resolved_path = path or Path.cwd() / _LOCAL_CONFIG_RELPATH
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: _Config = yaml.safe_load(config_file) or {}
        return raw


def _merge_effective_config(
    global_cfg: _Config,
    local_cfg: _Config,
) -> _Config:
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
    merged: _Config = {**global_cfg, **local_cfg}
    g_harness = global_cfg.get("harness")
    l_harness = local_cfg.get("harness")
    # Only deep-merge when BOTH are mappings. A scalar on either side is
    # an explicit whole-value override (legacy scalar form, or a project
    # that intentionally pins the whole harness key), so the shallow
    # ``{**global, **local}`` result already in ``merged`` is correct.
    if isinstance(g_harness, dict) and isinstance(l_harness, dict):
        combined: _Config = {**g_harness, **l_harness}
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


def load_effective_config() -> _Config:
    """Merge user and project config, with project values taking precedence.

    The ``harness`` mapping is deep-merged (per-harness sub-keys, local
    winning per-field) so a project's per-harness overrides augment —
    rather than replace — the user's global ones. Every other key is a
    shallow replace.
    """
    return _merge_effective_config(load_global_config(), load_local_config())


def save_global_config(
    settings: Mapping[str, object],
    *,
    deep_merge_keys: tuple[str, ...] = (),
    path: Path | None = None,
) -> None:
    """Merge *settings* into the user-level config and write it back atomically.

    A reusable, runtime-safe writer (the runner and host reader persist GitHub
    account preferences through it), mirroring the merge semantics of the CLI's
    ``_save_global_config`` minus its harness-scalar normalization. Every key in
    *settings* replaces its existing value wholesale, except keys in
    *deep_merge_keys*, whose mapping value is merged one level deep into the
    existing mapping for that key.

    :param settings: Key/value pairs to set.
    :param deep_merge_keys: Keys whose mapping value is merged one level deep
        rather than replacing the existing mapping.
    :param path: Config path override (defaults to :func:`global_config_path`).
    """
    resolved = path or global_config_path()
    cfg = load_global_config(resolved)
    for key, value in settings.items():
        if key in deep_merge_keys and isinstance(value, Mapping):
            existing = cfg.get(key)
            merged = dict(existing) if isinstance(existing, Mapping) else {}
            merged.update(value)
            cfg[key] = merged
        else:
            cfg[key] = value
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write can't truncate the user's config.
    tmp = resolved.with_name(resolved.name + ".tmp")
    with tmp.open("w") as config_file:
        yaml.safe_dump(cfg, config_file, default_flow_style=False, sort_keys=True)
    os.replace(tmp, resolved)


def _github_accounts(cfg: _Config) -> dict[str, object]:
    """Return the ``github.accounts`` mapping from *cfg* (empty when absent)."""
    github = cfg.get("github")
    if not isinstance(github, Mapping):
        return {}
    accounts = github.get("accounts")
    return dict(accounts) if isinstance(accounts, Mapping) else {}


def github_account_preference(workspace_key: str, path: Path | None = None) -> str | None:
    """Return the preferred ``gh`` login for a workspace, or ``None``.

    :param workspace_key: Stable per-workspace key (the main worktree path), used
        verbatim — a filesystem path, so not case-folded.
    :param path: Config path override (defaults to :func:`global_config_path`).
    """
    accounts = _github_accounts(load_global_config(path))
    value = accounts.get(workspace_key)
    return value if isinstance(value, str) and value else None


def set_github_account_preference(
    workspace_key: str,
    login: str | None,
    path: Path | None = None,
) -> None:
    """Persist (or clear, when *login* is falsy) the preferred login for a workspace.

    Stored under ``github.accounts`` keyed by the workspace's main worktree path,
    so the choice is shared across a repo's linked worktrees (which share one
    ``.git``) while separate clones stay distinct.

    :param workspace_key: Stable per-workspace key (the main worktree path).
    :param login: GitHub login to prefer, or ``None``/empty to clear the entry.
    :param path: Config path override (defaults to :func:`global_config_path`).
    """
    resolved = path or global_config_path()
    github = load_global_config(resolved).get("github")
    github_block = dict(github) if isinstance(github, Mapping) else {}
    accounts = _github_accounts({"github": github_block})
    key = workspace_key
    if login:
        accounts[key] = login
    else:
        accounts.pop(key, None)
    github_block["accounts"] = accounts
    save_global_config({"github": github_block}, path=resolved)


__all__ = [
    "github_account_preference",
    "global_config_path",
    "load_effective_config",
    "load_global_config",
    "load_local_config",
    "save_global_config",
    "set_github_account_preference",
]
