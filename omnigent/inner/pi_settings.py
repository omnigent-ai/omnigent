"""Helpers for seeding a managed Pi agent dir (``PI_CODING_AGENT_DIR``).

Session model/auth files live in an isolated agent root. Preserve supported
user resources there while keeping global instruction and session files out.
Linked resources remain shared and writable, as in standalone Pi.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, cast

_logger = logging.getLogger(__name__)

_Settings: TypeAlias = dict[str, object]

# Default global Pi agent root (``PI_CODING_AGENT_DIR`` when unset).
DEFAULT_PI_AGENT_DIR = Path.home() / ".pi" / "agent"

_PI_RESOURCE_TYPES = ("extensions", "skills", "prompts", "themes")
_PI_AGENT_RESOURCES = (
    *_PI_RESOURCE_TYPES,
    "npm",
    "git",
    "mcp.json",
    "mcp-auth.json",
    "keybindings.json",
)


def _read_settings_file(path: Path) -> _Settings:
    """Load a Pi ``settings.json`` file, returning ``{}`` when absent/invalid."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
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


def _relocate_resource_settings(
    settings: _Settings, agent_root: Path, managed_dir: Path
) -> _Settings:
    """Keep settings paths relative to the original user root, including exclusions."""
    settings = dict(settings)
    for kind in _PI_RESOURCE_TYPES:
        entries = settings.get(kind)
        if not isinstance(entries, list):
            continue
        relocated: list[object] = []
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                continue
            prefix = entry[0] if entry[0] in "!+-" else ""
            path = entry[len(prefix) :]
            if not path or path.startswith(("~", "builtin:")):
                relocated.append(entry)
                continue
            # Keep relative patterns for auto-discovery through the managed symlink.
            if prefix:
                relocated.append(entry)
                normalized_path = Path(os.path.normpath(path))
                if normalized_path.is_absolute() and normalized_path.is_relative_to(agent_root):
                    relocated.append(
                        prefix + str(managed_dir / normalized_path.relative_to(agent_root))
                    )
            relocated.append(prefix + str(agent_root / path))
        settings[kind] = list(dict.fromkeys(relocated))
    packages = settings.get("packages")
    if isinstance(packages, list):
        relocated_packages: list[object] = []
        for package in packages:
            source = package.get("source") if isinstance(package, dict) else package
            if isinstance(source, str) and source.startswith(("./", "../")):
                absolute = str(agent_root / source)
                package = (
                    {**package, "source": absolute} if isinstance(package, dict) else absolute
                )
            relocated_packages.append(package)
        settings["packages"] = relocated_packages
    return settings


def _symlink_agent_resources(
    managed_dir: Path,
    global_agent_dir: Path,
) -> None:
    """Link supported resources, retaining existing session-owned files."""
    for name in _PI_AGENT_RESOURCES:
        source = global_agent_dir / name
        target = managed_dir / name
        try:
            # A reused managed dir may still link to a previous root or a removed source.
            if target.is_symlink():
                if target.readlink() == source and source.exists():
                    continue
                target.unlink()
            elif target.exists():
                continue
            if source.exists():
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
    links supported resources, and preserves their relative settings paths.
    Linked resources are writable shared state; Pi may update MCP OAuth tokens.
    Global instruction files and model credentials are not linked.

    Project-scoped ``.pi/settings.json`` at the session cwd is still read by
    Pi at runtime and overrides these global settings per Pi's normal rules.

    :param managed_dir: Per-session agent root (already contains ``models.json``).
    :param overlay: Settings merged on top of the copied global file.
    :param global_agent_dir: Caller-resolved source root; defaults to
        :data:`DEFAULT_PI_AGENT_DIR`.
    """
    agent_root = global_agent_dir if global_agent_dir is not None else DEFAULT_PI_AGENT_DIR
    agent_root = Path(os.path.abspath(agent_root.expanduser()))
    if managed_dir.resolve() == agent_root.resolve():
        return
    settings = _relocate_resource_settings(
        _read_settings_file(agent_root / "settings.json"), agent_root, managed_dir.absolute()
    )
    if overlay:
        settings = _deep_merge_settings(settings, overlay)

    managed_dir.mkdir(parents=True, exist_ok=True)
    settings_path = managed_dir / "settings.json"
    try:
        if settings_path.is_symlink():
            settings_path.unlink()
        settings_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _logger.warning("Could not write managed Pi settings at %s: %s", settings_path, exc)
        return

    _symlink_agent_resources(managed_dir, agent_root)
