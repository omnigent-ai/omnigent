"""Host-owned inventory of the user-level MCP servers each harness loads.

Only names and non-secret metadata leave the inventory response: ``env``, headers, stdio
``command``/``args`` and full URLs routinely carry tokens, so they are dropped.

Per-server account state comes from ``mcp-auth-status.json`` (see
``docs/MCP_AUTH_STATUS.md``); it can only mark known servers and name an http(s) page.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urlsplit

from omnigent.host.frames import mcp_account_fields
from omnigent.spec.skill_sources import (
    SkillSourceContext,
    _enabled_plugin_keys,
    _plugin_asset_id,
    _plugin_install_paths,
    _read_json,
)

_logger = logging.getLogger(__name__)

MCP_INVENTORY_CACHE_TTL_SECONDS = 60.0
# Omnigent injects its own relay under this name; it is not user config.
OMNIGENT_RELAY_SERVER = "omnigent"
#: Where a host-side sign-in manager reports per-server account state (under ``$HOME``).
MCP_AUTH_STATUS_RELPATH = Path(".omnigent") / "mcp-auth-status.json"
#: Absolute path to use instead, for a deployment whose ``$HOME`` is read-only or
#: sandboxed and whose sign-in manager must write somewhere else.
MCP_AUTH_STATUS_ENV = "OMNIGENT_MCP_AUTH_STATUS_FILE"
#: The report is a short list; a larger file is treated as unreadable rather than parsed
#: on every poll.
MCP_AUTH_STATUS_MAX_BYTES = 64 * 1024
#: Signature of the file state already warned about, so a poll loop logs once per change.
_warned_auth_status: tuple[int, int] | None = None

McpServerSummary = dict[str, str]
#: ``(harness, name)`` → the ``auth_status`` / ``sign_in_url`` keys to add to its summary.
AuthStatusOverlay = dict[tuple[str, str], dict[str, str]]


@dataclass
class ConfiguredMcpServer:
    """Raw configuration stays on the host; only summary is sent to the server."""

    summary: McpServerSummary
    config: Mapping[str, object]
    plugin_root: Path | None = None


def _summary(
    name: object,
    config: object,
    harness: str,
    plugin: str | None = None,
    managed: bool = False,
) -> McpServerSummary | None:
    """Reduce one server entry to its name, transport, and at most a URL host.

    ``scope`` stays ``user`` for older servers; enterprise config is flagged via
    ``managed``.
    """
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
    if managed:
        summary["managed"] = "true"
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


def _managed_mcp_paths() -> tuple[Path, ...]:
    """Claude Code's enterprise MCP config, which sits beside its managed settings.

    Only locations that are absolute on this platform: the chain names every OS, and
    a Windows path read on POSIX would resolve against the working directory.
    """
    from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS

    return tuple(
        path.with_name("managed-mcp.json")
        for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS
        if path.is_absolute()
    )


def _managed_claude_servers() -> list[ConfiguredMcpServer]:
    """Servers an organization pins via Claude Code's ``managed-mcp.json``.

    Absent on an ordinary install; where it exists these are often the servers that
    need an interactive sign-in, so the inventory has to know about them.
    """
    out: list[ConfiguredMcpServer] = []
    # The first readable, object-shaped file decides, as Claude Code (and
    # ``claude_managed_gateway``) read this chain: a lower-precedence file is
    # ignored even when the winning one pins no servers.
    for path in _managed_mcp_paths():
        data = _read_config(path, "claude managed")
        if data is None:
            continue
        for name, config in _servers_in(data).items():
            summary = _summary(name, config, "claude", managed=True)
            if isinstance(config, Mapping) and summary is not None:
                out.append(ConfiguredMcpServer(summary, config))
        break
    return out


def _claude_servers(home: Path, env: Mapping[str, str]) -> list[ConfiguredMcpServer]:
    """Managed, user-scope, and enabled-plugin ``mcpServers`` for Claude Code."""
    out = _managed_claude_servers()
    # Enterprise policy outranks user config, so a managed server keeps its name.
    managed = {server.summary["name"] for server in out}
    configured = env.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(configured).expanduser() if configured else None
    data = _read_config((config_dir or home) / ".claude.json", "claude")
    out += [
        ConfiguredMcpServer(summary, config)
        for name, config in _servers_in(data).items()
        if isinstance(config, Mapping)
        and (summary := _summary(name, config, "claude")) is not None
        and summary["name"] not in managed
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
                    and summary["name"] not in managed
                ):
                    seen.add(summary["name"])
                    summary["source_id"] = _plugin_asset_id(key, "mcp", summary["name"])
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


def auth_status_path(home: Path) -> Path:
    """Where the sign-in manager's report is read from, honouring the env override.

    The override is used as given, so a relative path resolves against the host
    process's working directory; deployments should set an absolute one.
    """
    override = os.environ.get(MCP_AUTH_STATUS_ENV)
    return Path(override).expanduser() if override else home / MCP_AUTH_STATUS_RELPATH


def _read_auth_status(path: Path) -> Mapping[str, object] | None:
    """Read the status report, logging once per file change when it cannot be used."""
    global _warned_auth_status
    stat: os.stat_result | None = None
    data: object = None
    try:
        # O_NONBLOCK so a FIFO opens instead of waiting for a writer; the panel polls
        # this path every few seconds and a blocked worker thread would pile up.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            stat = os.fstat(fd)
            # Checked on the open descriptor, so nothing can be swapped in behind it.
            if S_ISREG(stat.st_mode) and stat.st_size <= MCP_AUTH_STATUS_MAX_BYTES:
                data = json.loads(os.read(fd, MCP_AUTH_STATUS_MAX_BYTES + 1))
        finally:
            os.close(fd)
    except FileNotFoundError:
        _warned_auth_status = None
        return None
    except (OSError, ValueError, RecursionError):
        # RecursionError is a RuntimeError, not a ValueError: deeply nested JSON
        # under the size cap would otherwise escape and fail the whole inventory.
        data = None
    if isinstance(data, Mapping):
        _warned_auth_status = None
        return data
    signature = (stat.st_mtime_ns, stat.st_size) if stat is not None else (0, 0)
    if _warned_auth_status != signature:
        _warned_auth_status = signature
        _logger.warning("Skipping unusable MCP auth status at %s", path)
    return None


def read_auth_status_overlay(home: Path) -> AuthStatusOverlay:
    """Read the sign-in manager's report; entries that fail validation are dropped."""
    data = _read_auth_status(auth_status_path(home))
    entries = data.get("servers") if data is not None else None
    if not isinstance(entries, list):
        return {}
    overlay: AuthStatusOverlay = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        harness, name = entry.get("harness"), entry.get("name")
        if not (isinstance(harness, str) and isinstance(name, str)):
            continue
        fields = mcp_account_fields(entry.get("status"), entry.get("sign_in_url"))
        if fields:
            overlay[(harness, name)] = fields
    return overlay


def _auth_status_overlay() -> AuthStatusOverlay:
    """The reported state, or none: the configured servers matter more than it does."""
    try:
        return read_auth_status_overlay(Path.home())
    except Exception:
        _logger.exception("Reading MCP auth status failed; reporting servers without it")
        return {}


def apply_auth_status(
    servers: list[McpServerSummary], overlay: AuthStatusOverlay
) -> list[McpServerSummary]:
    """Stamp each discovered server with its reported account state, if any."""
    for server in servers:
        fields = overlay.get((server["harness"], server["name"]))
        if fields is not None:
            server.update(fields)
    return servers


class HostMcpInventory:
    """Cache the inventory briefly and serialize scans off the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: tuple[float, list[McpServerSummary]] | None = None

    def discover(self) -> list[McpServerSummary]:
        """Return the cached inventory, rescanning after the TTL; account state is always fresh."""
        with self._lock:
            if self._cached is not None and self._cached[0] > time.monotonic():
                servers = [dict(server) for server in self._cached[1]]
            else:
                result = discover_mcp_servers()
                self._cached = (time.monotonic() + MCP_INVENTORY_CACHE_TTL_SECONDS, result)
                servers = [dict(server) for server in result]
        return apply_auth_status(servers, _auth_status_overlay())
