"""Installed Claude plugin metadata; configuration and executable contents stay local."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from omnigent.host.mcp_inventory import _servers_in, _summary
from omnigent.spec.parser import _discover_skills
from omnigent.spec.skill_sources import (
    _enabled_plugin_keys,
    _plugin_install_paths,
    _read_json,
    skill_source_context_from_env,
)

MAX_PLUGINS = 128
MAX_PLUGIN_ITEMS = 128
MAX_PLUGIN_NAME = 256
MAX_PLUGIN_DESCRIPTION = 1000


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(c if c.isprintable() else " " for c in value).strip()[:limit]


def discover_plugins() -> list[dict[str, object]]:
    """List installed plugins in the same home scope as the Harnesses inventory."""
    ctx = replace(
        skill_source_context_from_env(roots=(Path.home(),), harness="claude-native"),
        is_native=True,
    )
    enabled = _enabled_plugin_keys(ctx)
    plugins: list[dict[str, object]] = []
    for key, directory in _plugin_install_paths(ctx).items():
        name, _, marketplace = key.partition("@")
        if not name or not directory.is_dir():
            continue
        manifest = _read_json(directory / ".claude-plugin" / "plugin.json") or {}
        servers = dict(_servers_in(manifest))
        servers.update(_servers_in(_read_json(directory / ".mcp.json")))
        plugins.append(
            {
                "harness": "claude",
                "name": _text(name, MAX_PLUGIN_NAME),
                "marketplace": _text(marketplace, MAX_PLUGIN_NAME),
                "version": _text(manifest.get("version"), MAX_PLUGIN_NAME),
                "description": _text(manifest.get("description"), MAX_PLUGIN_DESCRIPTION),
                "enabled": key in enabled,
                "skills": [
                    _text(skill.name, MAX_PLUGIN_NAME)
                    for skill in _discover_skills(directory / "skills", skipped=[])[
                        :MAX_PLUGIN_ITEMS
                    ]
                ],
                "mcp_servers": [
                    _text(server, MAX_PLUGIN_NAME)
                    for server, config in servers.items()
                    if _summary(server, config, "claude") is not None
                ][:MAX_PLUGIN_ITEMS],
                "has_hooks": (directory / "hooks").is_dir() or bool(manifest.get("hooks")),
                "has_commands": (directory / "commands").is_dir()
                or bool(manifest.get("commands")),
            }
        )
        if len(plugins) >= MAX_PLUGINS:
            break
    return plugins
