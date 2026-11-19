"""Read Omnigent's user and project configuration."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

import yaml

from omnigent.preferences import (
    PreferenceContext,
    PreferenceKey,
    PreferenceKind,
    PreferenceLayer,
    PreferenceScope,
    ResolvedPreference,
    preferences_from_config,
    resolve_preferences,
)

_log = logging.getLogger(__name__)
_cwd_missing_warned = False

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


def _ignored_project_harness_keys(cfg: _Config) -> list[str]:
    """Name project harness overrides without exposing their command or arguments."""
    harness = cfg.get("harness")
    if not isinstance(harness, dict):
        return []
    return sorted(f"harness.{name}" for name in harness if name != "default")


def _merge_effective_config(
    global_cfg: _Config,
    local_cfg: _Config,
) -> _Config:
    """Merge project defaults while retaining user-owned harness launch settings.

    A project may select the default harness. Server selection, executables and
    arguments come from user configuration or explicit command-line options.

    :param global_cfg: User-level config (``~/.omnigent/config.yaml``).
    :param local_cfg: Project-level config (``.omnigent/config.yaml``).
    :returns: The merged effective config dict.
    """
    merged: _Config = {
        **global_cfg,
        **{key: value for key, value in local_cfg.items() if key not in {"server", "harness"}},
    }
    g_harness = global_cfg.get("harness")
    l_harness = local_cfg.get("harness")
    ignored = _ignored_project_harness_keys(local_cfg)
    if ignored and l_harness != g_harness:
        _log.warning(
            "Ignoring project harness launch overrides: %s. "
            "Move these settings to user config %s.",
            ", ".join(ignored),
            global_config_path(),
        )
    if isinstance(l_harness, str):
        l_harness = {"default": l_harness}
    if isinstance(l_harness, dict) and "default" in l_harness:
        trusted = g_harness if isinstance(g_harness, dict) else {}
        merged["harness"] = {**trusted, "default": l_harness["default"]}
    return merged


@dataclass(frozen=True)
class ResolvedConfig:
    config: _Config
    preferences: Mapping[PreferenceKey, ResolvedPreference] = field(default_factory=dict)


def resolve_effective_config(
    global_cfg: _Config,
    local_cfg: _Config,
    *,
    preference_context: PreferenceContext | None = None,
    preference_layers: Sequence[PreferenceLayer] = (),
) -> ResolvedConfig:
    """Resolve legacy config, or explicitly adopted workspace preferences.

    Without a context, preserve legacy merging exactly. Supplying a context is
    an explicit adoption boundary: global portable values are no longer inputs.
    Callers must complete import/migration before opting in; this function does
    not infer user intent from YAML or write resolved values back to it.

    Project YAML remains an override, while commands, credentials, and unrelated
    settings retain their existing merge rules. Plugin choices remain in the
    provenance map for native adapters; they are not written into global YAML.
    """
    if preference_context is None:
        if preference_layers:
            raise ValueError("preference layers require an authenticated context")
        return ResolvedConfig(_merge_effective_config(global_cfg, local_cfg))
    if any(layer.scope == PreferenceScope.PROJECT for layer in preference_layers):
        raise ValueError("project preferences must come from the selected project config")
    project = PreferenceLayer(
        PreferenceScope.PROJECT,
        preference_context.identity,
        {
            key: value
            for key, value in preferences_from_config(local_cfg).items()
            if key.kind != PreferenceKind.HARNESS_ARGS
        },
    )
    resolved = resolve_preferences(preference_context, (*preference_layers, project))
    resolved = {
        key: item
        for key, item in resolved.items()
        if item.source in (PreferenceScope.SESSION, PreferenceScope.PROJECT)
        or not _project_masks_preference(local_cfg, key)
    }
    config = _without_portable_preferences(_merge_effective_config(global_cfg, local_cfg))
    _apply_preferences(config, resolved)
    return ResolvedConfig(config, resolved)


def _project_masks_preference(cfg: _Config, key: PreferenceKey) -> bool:
    """Apply explicit project replacements to lower scopes by canonical key."""
    if key.kind == PreferenceKind.MODEL:
        return key.name in cfg
    if key.kind != PreferenceKind.HARNESS:
        # Harness launch arguments are user-owned, matching _merge_effective_config.
        return False
    harness = cfg.get("harness")
    return isinstance(harness, str) or (isinstance(harness, dict) and "default" in harness)


def _apply_preferences(
    config: _Config, resolved: Mapping[PreferenceKey, ResolvedPreference]
) -> None:
    """Project requested values into the existing launch-config shape."""
    for key, preference in resolved.items():
        if key.kind == PreferenceKind.MODEL:
            assert isinstance(key.name, str)
            config[key.name] = preference.value
        elif key.kind in (PreferenceKind.HARNESS, PreferenceKind.HARNESS_ARGS):
            harness = config.setdefault("harness", {})
            assert isinstance(harness, dict)
            if key.kind == PreferenceKind.HARNESS:
                harness["default"] = preference.value
            else:
                entry = harness.get(key.name)
                entry = dict(entry) if isinstance(entry, dict) else {}
                assert isinstance(preference.value, tuple)
                entry["args"] = list(preference.value)
                harness[key.name] = entry


def _without_portable_preferences(cfg: _Config) -> _Config:
    """Keep local-only settings without mutating either source dictionary."""
    config = deepcopy(cfg)
    for name in ("model", "opencode_model"):
        config.pop(name, None)
    harness = config.get("harness")
    if isinstance(harness, dict):
        harness.pop("default", None)
        for entry in harness.values():
            if isinstance(entry, dict):
                entry.pop("args", None)
    else:
        config.pop("harness", None)
    return config


def load_effective_config(
    *,
    workspace: str | Path | None = None,
    preference_context: PreferenceContext | None = None,
    preference_layers: Sequence[PreferenceLayer] = (),
) -> _Config:
    """Merge global and project config, preserving user-owned launch settings.

    Project values take precedence except for server selection and harness
    executables and arguments. When supplied, *workspace* selects the project
    config directory instead of the process cwd.
    """
    local_path = Path(workspace) / _LOCAL_CONFIG_RELPATH if workspace is not None else None
    return resolve_effective_config(
        load_global_config(),
        load_local_config(local_path),
        preference_context=preference_context,
        preference_layers=preference_layers,
    ).config


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
    "resolve_effective_config",
    "save_global_config",
    "set_github_account_preference",
]
