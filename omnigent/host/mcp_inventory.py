"""Host-owned inventory of the user-level MCP servers each harness loads.

Only names and non-secret metadata leave the inventory response: ``env``, headers, stdio
``command``/``args`` and full URLs routinely carry tokens, so they are dropped.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import tomllib

from omnigent.spec.skill_sources import (
    SkillSourceContext,
    _enabled_plugin_keys,
    _plugin_install_paths,
    _read_json,
)

_logger = logging.getLogger(__name__)

MCP_INVENTORY_CACHE_TTL_SECONDS = 60.0
# Omnigent injects its own relay under this name; it is not user config.
OMNIGENT_RELAY_SERVER = "omnigent"

McpServerSummary = dict[str, str]


@dataclass
class ConfiguredMcpServer:
    """Raw configuration stays on the host; only summary is sent to the server."""

    summary: McpServerSummary
    config: Mapping[str, object]
    plugin_root: Path | None = None


def _summary(
    name: object, config: object, harness: str, plugin: str | None = None
) -> McpServerSummary | None:
    """Reduce one server entry to its name, transport, and at most a URL host."""
    if not isinstance(name, str) or not name or name == OMNIGENT_RELAY_SERVER:
        return None
    if not isinstance(config, Mapping) or config.get("enabled") is False:
        return None
    url = config.get("url")
    kind = config.get("type")
    if isinstance(url, str) and url:
        transport = "http"
    elif isinstance(config.get("command"), str) or kind == "stdio":
        transport = "stdio"
    elif kind in ("http", "sse", "streamable-http"):
        transport = "http"
    else:
        return None
    summary = {"name": name, "harness": harness, "transport": transport, "scope": "user"}
    if plugin is not None:
        summary["plugin"] = plugin
    if transport == "http" and isinstance(url, str):
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if host:
            summary["url_host"] = host
    return summary


def _servers_in(data: Mapping[str, object] | None) -> Mapping[str, object]:
    """Return the ``mcpServers`` table of a JSON config, or an empty mapping."""
    servers = data.get("mcpServers") if data is not None else None
    return servers if isinstance(servers, Mapping) else {}


def _read_config(path: Path, harness: str) -> Mapping[str, object] | None:
    """Read a JSON config, logging (not raising) when it exists but is unreadable."""
    data = _read_json(path)
    if data is None and path.exists():
        _logger.warning("Skipping unreadable %s MCP config at %s", harness, path)
    return data


def _claude_servers(home: Path, env: Mapping[str, str]) -> list[ConfiguredMcpServer]:
    """User-scope ``mcpServers`` plus servers bundled by enabled Claude plugins."""
    configured = env.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(configured).expanduser() if configured else None
    data = _read_config((config_dir or home) / ".claude.json", "claude")
    out = [
        ConfiguredMcpServer(summary, config)
        for name, config in _servers_in(data).items()
        if isinstance(config, Mapping)
        and (summary := _summary(name, config, "claude")) is not None
    ]
    ctx = SkillSourceContext(
        roots=(),
        home=home,
        skills_filter="all",
        bundle_dir=None,
        claude_config_dir=config_dir,
        is_native=True,
    )
    for key, install_path in _plugin_install_paths(ctx, _enabled_plugin_keys(ctx)).items():
        plugin = key.split("@", 1)[0]
        manifest = _read_json(install_path / ".claude-plugin" / "plugin.json")
        tables = [_servers_in(_read_json(install_path / ".mcp.json")), _servers_in(manifest)]
        seen: set[str] = set()
        for table in tables:
            for name, config in table.items():
                summary = _summary(name, config, "claude", plugin)
                if (
                    isinstance(config, Mapping)
                    and summary is not None
                    and summary["name"] not in seen
                ):
                    seen.add(summary["name"])
                    out.append(ConfiguredMcpServer(summary, config, install_path))
    return out


def _codex_servers() -> list[ConfiguredMcpServer]:
    """``[mcp_servers.*]`` tables from the user's Codex ``config.toml``."""
    from omnigent.inner.codex_executor import _codex_home_config_source_from_env

    path = _codex_home_config_source_from_env() / "config.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        _logger.warning("Skipping unreadable codex MCP config at %s", path)
        return []
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return []
    return [
        ConfiguredMcpServer(summary, table)
        for name, table in servers.items()
        if isinstance(table, Mapping) and (summary := _summary(name, table, "codex")) is not None
    ]


def _cursor_servers(home: Path) -> list[ConfiguredMcpServer]:
    """Global ``~/.cursor/mcp.json``, shared by the Cursor app and ``cursor-agent``."""
    data = _read_config(home / ".cursor" / "mcp.json", "cursor")
    return [
        ConfiguredMcpServer(summary, config)
        for name, config in _servers_in(data).items()
        if isinstance(config, Mapping)
        and (summary := _summary(name, config, "cursor")) is not None
    ]


def configured_mcp_servers() -> list[ConfiguredMcpServer]:
    """List user-level MCP servers across Claude, Codex, and Cursor on this host.

    Each harness is read independently, so a missing or broken config for one
    never hides another's servers.
    """
    home = Path.home()
    readers = (
        ("claude", lambda: _claude_servers(home, os.environ)),
        ("codex", _codex_servers),
        ("cursor", lambda: _cursor_servers(home)),
    )
    out: list[ConfiguredMcpServer] = []
    for harness, read in readers:
        try:
            out.extend(read())
        except Exception:
            _logger.exception("MCP inventory failed for %s", harness)
    return out


def discover_mcp_servers() -> list[McpServerSummary]:
    return [server.summary for server in configured_mcp_servers()]


class HostMcpInventory:
    """Cache the inventory briefly and serialize scans off the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: tuple[float, list[McpServerSummary]] | None = None

    def discover(self) -> list[McpServerSummary]:
        """Return the cached inventory, rescanning after the TTL."""
        with self._lock:
            if self._cached is not None and self._cached[0] > time.monotonic():
                return [dict(server) for server in self._cached[1]]
            result = discover_mcp_servers()
            self._cached = (time.monotonic() + MCP_INVENTORY_CACHE_TTL_SECONDS, result)
            return [dict(server) for server in result]
