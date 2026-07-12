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


def load_effective_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """Merge user and project config, with project values taking precedence."""
    return {**load_global_config(), **load_local_config()}


__all__ = [
    "global_config_path",
    "load_effective_config",
    "load_global_config",
    "load_local_config",
]
