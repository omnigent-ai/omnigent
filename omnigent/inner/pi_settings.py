"""Helpers for seeding a managed Pi agent dir (``PI_CODING_AGENT_DIR``).

When the pi harness runs in gateway mode it relocates Pi's agent root to a
per-session temp directory (for ``models.json``). That hides everything else in
the user's global ``~/.pi/agent`` — settings, MCP servers, skills, extensions,
packages. This module copies the global settings into the managed dir and
symlinks the rest of the user's agent-dir entries, so a managed session sees
the same Pi setup as a plain ``pi`` run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, cast

_logger = logging.getLogger(__name__)

_Settings: TypeAlias = dict[str, object]

# Default global Pi agent root (``PI_CODING_AGENT_DIR`` when unset).
DEFAULT_PI_AGENT_DIR = Path.home() / ".pi" / "agent"

# Global agent-dir entries NOT linked into the managed dir; every other entry is
# (``mcp.json``, ``skills/``, ``extensions/``, ``npm/``, ...), including ones newer
# Pi versions add.
_UNSHARED_AGENT_DIR_ENTRIES: frozenset[str] = frozenset(
    {
        # Written per session by Omnigent; Pi keeps models-store.json beside models.json.
        "models.json",
        "models-store.json",
        "settings.json",
        # Model-provider logins: managed sessions use the provider Omnigent configures.
        "auth.json",
        "oauth.json",
        # Per-run state and logs.
        "sessions",
        "tmp",
        "crashes.json",
        "mcp.log",
        "pi-debug.log",
    }
)


def _read_settings_file(path: Path) -> _Settings:
    """Load a Pi ``settings.json`` file, returning ``{}`` when absent/invalid."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("Ignoring unreadable Pi settings at %s: %s", path, exc)
        return {}
    return cast(_Settings, raw) if isinstance(raw, dict) else {}


def _deep_merge_settings(base: Mapping[str, object], overlay: Mapping[str, object]) -> _Settings:
    """Merge *overlay* onto *base* using Pi's nested-object merge semantics."""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


def _symlink_agent_resource_dirs(
    managed_dir: Path,
    global_agent_dir: Path,
) -> None:
    """Symlink shareable entries of *global_agent_dir* into *managed_dir*."""
    if not global_agent_dir.is_dir():
        return
    for source in sorted(global_agent_dir.iterdir()):
        if source.name in _UNSHARED_AGENT_DIR_ENTRIES:
            continue
        target = managed_dir / source.name
        if target.exists():
            continue
        try:
            target.symlink_to(source, target_is_directory=source.is_dir())
        except OSError as exc:
            _logger.warning(
                "Could not symlink Pi agent resource %s -> %s: %s",
                target,
                source,
                exc,
            )


def prepare_managed_pi_agent_dir(
    managed_dir: Path,
    *,
    overlay: Mapping[str, object] | None = None,
    global_agent_dir: Path | None = None,
) -> None:
    """
    Seed a managed ``PI_CODING_AGENT_DIR`` with the user's global Pi settings.

    Copies (merges) ``settings.json`` from the user's global agent dir into
    *managed_dir*, applies *overlay* (e.g. Omnigent retry policy), and
    symlinks the other global entries — ``mcp.json``, ``skills/``,
    ``extensions/``, ``npm/``, etc. — except those in
    :data:`_UNSHARED_AGENT_DIR_ENTRIES`. Omnigent never writes to the user's
    ``~/.pi/agent``; Pi may update linked files (e.g. refreshed MCP OAuth
    tokens) as it would in a plain ``pi`` run.

    Project-scoped ``.pi/settings.json`` at the session cwd is still read by
    Pi at runtime and overrides these global settings per Pi's normal rules.

    :param managed_dir: Per-session agent root (already contains ``models.json``).
    :param overlay: Settings merged on top of the copied global file.
    :param global_agent_dir: Override for tests; defaults to
        :data:`DEFAULT_PI_AGENT_DIR`.
    """
    agent_root = global_agent_dir if global_agent_dir is not None else DEFAULT_PI_AGENT_DIR
    settings = _read_settings_file(agent_root / "settings.json")
    if overlay:
        settings = _deep_merge_settings(settings, overlay)

    managed_dir.mkdir(parents=True, exist_ok=True)
    settings_path = managed_dir / "settings.json"
    try:
        settings_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _logger.warning("Could not write managed Pi settings at %s: %s", settings_path, exc)
        return

    _symlink_agent_resource_dirs(managed_dir, agent_root)
