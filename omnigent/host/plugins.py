"""Installed Claude plugin metadata; configuration and executable contents stay local."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from omnigent.host.mcp_inventory import _servers_in, _summary
from omnigent.spec.parser import _discover_skills
from omnigent.spec.skill_sources import (
    _enabled_plugin_keys,
    _plugin_asset_id,
    _plugin_install_paths,
    _read_json,
    skill_source_context_from_env,
)
from omnigent.spec.types import SkillSpec

MAX_PLUGINS = 128
MAX_PLUGIN_ITEMS = 128
MAX_PLUGIN_NAME = 256
MAX_PLUGIN_DESCRIPTION = 1000


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(c if c.isprintable() else " " for c in value).strip()[:limit]


def _plugin_skills(key: str, directory: Path) -> list[tuple[str, SkillSpec]]:
    return [
        (_plugin_asset_id(key, "skill", skill.skill_dir.relative_to(directory).as_posix()), skill)
        for skill in _discover_skills(directory / "skills", skipped=[])[:MAX_PLUGIN_ITEMS]
        if skill.skill_dir is not None
    ]


def discover_plugins() -> list[dict[str, object]]:
    """List installed plugins using the same user scope as the MCP inventory."""
    ctx = replace(
        skill_source_context_from_env(roots=(), harness="claude-native"),
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
        skills = _plugin_skills(key, directory)
        server_names = [
            server
            for server, config in servers.items()
            if _summary(server, config, "claude") is not None
        ][:MAX_PLUGIN_ITEMS]
        plugins.append(
            {
                "id": _plugin_asset_id(key, "plugin"),
                "harness": "claude",
                "name": _text(name, MAX_PLUGIN_NAME),
                "marketplace": _text(marketplace, MAX_PLUGIN_NAME),
                "version": _text(manifest.get("version"), MAX_PLUGIN_NAME),
                "description": _text(manifest.get("description"), MAX_PLUGIN_DESCRIPTION),
                "enabled": key in enabled,
                "skills": [_text(skill.name, MAX_PLUGIN_NAME) for _, skill in skills],
                "skill_entries": [
                    {"id": source_id, "name": _text(skill.name, MAX_PLUGIN_NAME)}
                    for source_id, skill in skills
                ],
                "mcp_servers": [_text(server, MAX_PLUGIN_NAME) for server in server_names],
                "mcp_entries": [
                    {
                        "id": _plugin_asset_id(key, "mcp", server),
                        "name": _text(server, MAX_PLUGIN_NAME),
                    }
                    for server in server_names
                ],
                "has_hooks": (directory / "hooks").is_dir() or bool(manifest.get("hooks")),
                "has_commands": (directory / "commands").is_dir()
                or bool(manifest.get("commands")),
            }
        )
        if len(plugins) >= MAX_PLUGINS:
            break
    return plugins
