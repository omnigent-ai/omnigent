"""Read Omnigent's user and project configuration."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

import yaml

_log = logging.getLogger(__name__)
_cwd_missing_warned = False
_harness_override_warned = False

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
    global _cwd_missing_warned
    if path is None:
        try:
            resolved_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
        except OSError:
            # A runner can outlive its working directory; no local config to load.
            if not _cwd_missing_warned:
                _cwd_missing_warned = True
                _log.warning("load_local_config: cwd no longer exists; skipping project config")
            return {}
    else:
        resolved_path = path
    if not resolved_path.exists():
        return {}
    with resolved_path.open() as config_file:
        raw: _Config = yaml.safe_load(config_file) or {}
        return raw


def _project_harness_default(harness: object) -> str | None:
    """Return the project-selected default harness when it is an accepted value.

    A project may select the default harness only as a non-empty string, written
    either as a scalar ``harness: <id>`` or as ``harness.default: <id>``. Any
    other value is rejected so untrusted project config cannot steer the launch
    away from the user's harness selection.
    """
    normalized = {"default": harness} if isinstance(harness, str) else harness
    if not isinstance(normalized, dict):
        return None
    default = normalized.get("default")
    return default if isinstance(default, str) and default else None


def _project_harness_override_keys(harness: object) -> list[str]:
    """Return the project harness mapping keys other than ``default``, sorted.

    These are per-harness launch overrides (``command``/``args``) that the merge
    discards because executables and arguments are user-owned.
    """
    if not isinstance(harness, dict):
        return []
    return sorted(key for key in harness if isinstance(key, str) and key != "default")


def _merge_effective_config(
    global_cfg: _Config,
    local_cfg: _Config,
) -> _Config:
    """Merge project defaults while retaining user-owned harness launch settings.

    A project may select the default harness, but executables and arguments
    come from user configuration or explicit command-line options.

    :param global_cfg: User-level config (``~/.omnigent/config.yaml``).
    :param local_cfg: Project-level config (``.omnigent/config.yaml``).
    :returns: The merged effective config dict.
    """
    global _harness_override_warned
    merged: _Config = {**global_cfg, **{k: v for k, v in local_cfg.items() if k != "harness"}}
    g_harness = global_cfg.get("harness")
    l_harness = local_cfg.get("harness")
    # The resolver (and ``config list``) see only the merged result, so warn once
    # per process when project launch overrides are dropped; otherwise a project
    # that relied on them would stop taking effect with no explanation.
    override_keys = _project_harness_override_keys(l_harness)
    if override_keys and not _harness_override_warned:
        _harness_override_warned = True
        _log.warning(
            "ignoring project harness launch override(s): %s (command/args are user-owned)",
            ", ".join(override_keys),
        )
    # A project may only select the default harness, and only as a non-empty
    # string; a malformed or empty default is ignored so untrusted project
    # config fails safe and keeps the user's harness selection.
    l_default = _project_harness_default(l_harness)
    if l_default:
        trusted = g_harness if isinstance(g_harness, dict) else {}
        merged["harness"] = {**trusted, "default": l_default}
    return merged


def load_effective_config(*, workspace: str | Path | None = None) -> _Config:
    """Merge global and project config, preserving user-owned launch settings.

    Project values take precedence except for harness executables and arguments.
    When supplied, *workspace* selects the project config directory instead
    of the process cwd.
    """
    local_path = Path(workspace) / _LOCAL_CONFIG_RELPATH if workspace is not None else None
    return _merge_effective_config(load_global_config(), load_local_config(local_path))


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
