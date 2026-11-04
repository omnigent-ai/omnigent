"""Give managed Pi sessions the MCP servers the host configures for other harnesses.

Pi reads user-level servers from ``mcp.json`` in its agent dir. A managed agent
dir links the user's file; when the host inventory (Claude Code, Codex, Cursor)
knows servers that file lacks, the managed dir gets a merged copy instead. The
user's own entries win on a name clash, and their agent dir is never written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from omnigent.host.mcp_inventory import ConfiguredMcpServer

_logger = logging.getLogger(__name__)

PiMcpServer: TypeAlias = dict[str, object]

PI_MCP_CONFIG_NAME = "mcp.json"
# Holds the SHA-256 of the last generated copy, so a copy Pi or the user changed is kept.
_GENERATED_DIGEST_NAME = ".omnigent-mcp.json.sha256"
_PI_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
# Pi treats a user entry without these as an override of a server defined elsewhere.
_PI_TRANSPORT_KEYS = ("command", "url", "type")
_PI_OVERRIDE_KEYS = ("enabled", "exposure", "toolExposure")
_PI_OAUTH_KEYS = (
    "clientId",
    "clientSecret",
    "callbackPort",
    "callbackUrl",
    "scope",
    "clientName",
    "clientRegistration",
    "authServerMetadataUrl",
)


def _pi_server_key(name: str) -> str:
    """Pi gives names that differ only in ``-`` and ``_`` one tool namespace."""
    return name.replace("-", "_")


def _pi_literal(value: str) -> str:
    """Escape *value* so Pi neither interpolates ``$NAME`` nor runs a leading ``!``."""
    escaped = value.replace("$", "$$")
    return "$" + escaped if escaped.startswith("!") else escaped


def _pi_template(
    value: str, harness: str, inline: Mapping[str, str], pi_env: Mapping[str, str]
) -> str:
    """Rewrite an ``env``/``headers`` value as a Pi template that keeps variables unresolved.

    Variables in *inline* (non-secret paths such as a plugin root) are written as
    literals, so Pi needs no extra environment. Raises :class:`ValueError` for a
    placeholder Pi cannot express.
    """
    parts: list[str] = []
    position = 0
    for match in _PLACEHOLDER.finditer(value):
        parts.append(value[position : match.start()].replace("$", "$$"))
        position = match.end()
        key = match[1]
        if harness == "cursor":
            if key == "userHome":
                parts.append(str(Path.home()).replace("$", "$$"))
                continue
            if not key.startswith("env:"):
                raise ValueError("unsupported substitution")
            key = key[4:]
        name, separator, default = key.partition(":-")
        if name in inline:
            parts.append(inline[name].replace("$", "$$"))
        elif not _ENV_NAME.match(name):
            raise ValueError("unsupported substitution")
        elif separator and not pi_env.get(name):
            parts.append(default.replace("$", "$$"))
        else:
            parts.append("${" + name + "}")
    parts.append(value[position:].replace("$", "$$"))
    rendered = "".join(parts)
    return "$" + rendered if rendered.startswith("!") else rendered


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("invalid string list")
    return value


def _from_json_config(
    raw: Mapping[str, object], harness: str, plugin_root: Path | None, pi_env: Mapping[str, str]
) -> PiMcpServer:
    """Translate a Claude Code or Cursor ``mcpServers`` entry into Pi's shape."""
    from omnigent.host.mcp_tools import _expand, _strings

    if raw.get("type") == "sse":
        raise ValueError("Pi does not support the SSE transport")
    inline = {"CLAUDE_PLUGIN_ROOT": str(plugin_root)} if plugin_root is not None else {}
    env = {**pi_env, **inline}
    entry: PiMcpServer = {}
    url = raw.get("url")
    if isinstance(url, str) and url:
        # Pi interpolates only env, headers and OAuth secrets, so the URL is resolved here.
        entry["url"] = _expand(url, harness, env)
        headers = _strings(raw.get("headers", {}))
        if headers:
            entry["headers"] = {
                k: _pi_template(v, harness, inline, pi_env) for k, v in headers.items()
            }
        oauth = raw.get("oauth")
        if isinstance(oauth, Mapping):
            settings = {key: oauth[key] for key in _PI_OAUTH_KEYS if key in oauth}
            # Claude Code spells Pi's space-separated ``scope`` as ``scopes``.
            if "scope" not in settings and isinstance(oauth.get("scopes"), str):
                settings["scope"] = oauth["scopes"]
            if settings:
                entry["oauth"] = settings
        return entry
    command = raw.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError("invalid stdio configuration")
    entry["command"] = _expand(command, harness, env)
    args = _string_list(raw.get("args", []))
    if args:
        entry["args"] = [_expand(arg, harness, env) for arg in args]
    variables = _strings(raw.get("env", {}))
    if variables:
        entry["env"] = {k: _pi_template(v, harness, inline, pi_env) for k, v in variables.items()}
    return entry


def _codex_tool_exposure(raw: Mapping[str, object]) -> dict[str, str]:
    """Carry Codex's per-server tool allow/deny lists over as Pi exposures."""
    exposure: dict[str, str] = {}
    if "enabled_tools" in raw:
        exposure["*"] = "hidden"
        exposure.update(dict.fromkeys(_string_list(raw["enabled_tools"]), "codemode"))
    if "disabled_tools" in raw:
        exposure.update(dict.fromkeys(_string_list(raw["disabled_tools"]), "hidden"))
    return exposure


def _from_codex_config(raw: Mapping[str, object]) -> PiMcpServer:
    """Translate a Codex ``[mcp_servers.*]`` table; Codex values are all literal."""
    from omnigent.host.mcp_tools import _strings

    entry: PiMcpServer = {}
    url = raw.get("url")
    if isinstance(url, str) and url:
        entry["url"] = url
        headers = {k: _pi_literal(v) for k, v in _strings(raw.get("http_headers", {})).items()}
        for header, variable in _strings(raw.get("env_http_headers", {})).items():
            if not _ENV_NAME.match(variable):
                raise ValueError("invalid environment name")
            headers[header] = "${" + variable + "}"
        token_variable = raw.get("bearer_token_env_var")
        if token_variable is not None:
            if not isinstance(token_variable, str) or not _ENV_NAME.match(token_variable):
                raise ValueError("invalid environment name")
            headers["Authorization"] = "Bearer ${" + token_variable + "}"
        if headers:
            entry["headers"] = headers
    else:
        command = raw.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("invalid stdio configuration")
        entry["command"] = command
        args = _string_list(raw.get("args", []))
        if args:
            entry["args"] = args
        variables = _strings(raw.get("env", {}))
        if variables:
            entry["env"] = {k: _pi_literal(v) for k, v in variables.items()}
        cwd = raw.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ValueError("invalid working directory")
            entry["cwd"] = str(Path.home() / Path(cwd).expanduser())
    timeout = raw.get("tool_timeout_sec")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        entry["timeout"] = timeout
    tool_exposure = _codex_tool_exposure(raw)
    if tool_exposure:
        entry["toolExposure"] = tool_exposure
    return entry


def pi_mcp_servers_from_host(
    servers: Iterable[ConfiguredMcpServer], pi_env: Mapping[str, str] | None = None
) -> dict[str, PiMcpServer]:
    """Translate host-inventory servers into Pi ``mcpServers`` entries.

    The first server for a Pi name wins, in inventory order (Claude Code, then
    Codex, then Cursor). Servers Pi cannot load are left out and logged.
    Placeholders resolve against *pi_env*, the environment Pi will run with
    (default ``os.environ``), never against runner auth secrets.
    """
    from omnigent.runner.identity import strip_runner_auth_secrets

    resolvable = strip_runner_auth_secrets(os.environ if pi_env is None else pi_env)
    translated: dict[str, PiMcpServer] = {}
    taken: set[str] = set()
    for server in servers:
        name = server.summary["name"]
        harness = server.summary["harness"]
        if not _PI_SERVER_NAME.match(name) or _pi_server_key(name) in taken:
            continue
        try:
            if harness == "codex":
                entry = _from_codex_config(server.config)
            else:
                entry = _from_json_config(server.config, harness, server.plugin_root, resolvable)
        except (ValueError, TypeError) as exc:
            _logger.info("Not adding %s MCP server %r to Pi sessions: %s", harness, name, exc)
            continue
        taken.add(_pi_server_key(name))
        translated[name] = entry
    return translated


def _host_mcp_servers() -> list[ConfiguredMcpServer]:
    from omnigent.host.mcp_inventory import configured_mcp_servers

    return configured_mcp_servers()


def _read_user_config(path: Path) -> dict[str, object] | None:
    """Return the user's Pi MCP config (``{}`` when absent), or ``None`` when unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _logger.warning("Not merging host MCP servers into unreadable %s: %s", path, exc)
        return None
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        _logger.warning("Not merging host MCP servers into %s: no mcpServers object", path)
        return None
    return data


def _is_override(entry: object) -> bool:
    return isinstance(entry, Mapping) and not any(key in entry for key in _PI_TRANSPORT_KEYS)


def merge_pi_mcp_config(
    user_config: Mapping[str, object], host_servers: Mapping[str, PiMcpServer]
) -> dict[str, object] | None:
    """Add host servers to *user_config*, or return ``None`` when it already has them all.

    A user entry with a transport replaces the host server of the same Pi name.
    One with only ``enabled``, ``exposure`` or ``toolExposure`` adjusts it, as a
    project ``.pi/mcp.json`` override does in Pi.
    """
    user_servers = user_config.get("mcpServers", {})
    if not isinstance(user_servers, Mapping):
        raise ValueError("mcpServers must be an object")
    defined = {_pi_server_key(n) for n, entry in user_servers.items() if not _is_override(entry)}
    overrides = {_pi_server_key(n): e for n, e in user_servers.items() if _is_override(e)}
    added: dict[str, object] = {}
    for name, entry in host_servers.items():
        key = _pi_server_key(name)
        if key in defined:
            continue
        override = overrides.get(key)
        adjustments = (
            {field: override[field] for field in _PI_OVERRIDE_KEYS if field in override}
            if isinstance(override, Mapping)
            else {}
        )
        added[name] = {**entry, **adjustments}
    if not added:
        return None
    merged_keys = {_pi_server_key(name) for name in added}
    kept = {n: e for n, e in user_servers.items() if _pi_server_key(n) not in merged_keys}
    return {**user_config, "mcpServers": {**added, **kept}}


def _write_private_file(path: Path, content: bytes) -> None:
    """Replace *path* (including a symlink, never its target) with a 0600 file."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_generated_copy(target: Path, digest: Path, data: Mapping[str, object]) -> None:
    """Write *data* as the managed ``mcp.json`` and record its digest."""
    content = (json.dumps(data, indent=2) + "\n").encode()
    _write_private_file(target, content)
    try:
        _write_private_file(digest, hashlib.sha256(content).hexdigest().encode())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _is_session_owned(target: Path, digest: Path) -> bool:
    """Whether *target* is a regular file that this module did not write unchanged."""
    if target.is_symlink() or not target.exists():
        return False
    try:
        recorded = digest.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return True
    return recorded != hashlib.sha256(target.read_bytes()).hexdigest()


def _restore_user_link(target: Path, source: Path, digest: Path) -> None:
    """Drop a copy generated by an earlier launch so the user's file is linked again."""
    digest.unlink(missing_ok=True)
    if target.is_symlink():
        return
    target.unlink(missing_ok=True)
    if source.exists():
        target.symlink_to(source)


def sync_managed_pi_mcp_config(
    managed_dir: Path, agent_root: Path, pi_env: Mapping[str, str] | None = None
) -> None:
    """Give a managed Pi agent dir the host's MCP servers alongside the user's own.

    An ``mcp.json`` that Pi or the user wrote in the managed dir is kept as is.
    Failures are logged and never block a Pi launch.
    """
    target = managed_dir / PI_MCP_CONFIG_NAME
    source = agent_root / PI_MCP_CONFIG_NAME
    digest = managed_dir / _GENERATED_DIGEST_NAME
    try:
        if _is_session_owned(target, digest):
            _logger.info("Keeping the session's own Pi MCP config %s", target)
            return
        user_config = _read_user_config(source)
        merged = (
            None
            if user_config is None
            else merge_pi_mcp_config(
                user_config, pi_mcp_servers_from_host(_host_mcp_servers(), pi_env)
            )
        )
        if merged is None:
            _restore_user_link(target, source, digest)
            return
        _write_generated_copy(target, digest, merged)
    except Exception:
        _logger.exception("Could not add host MCP servers to the Pi agent dir %s", managed_dir)
        _fall_back_to_user_link(target, source, digest)


def _fall_back_to_user_link(target: Path, source: Path, digest: Path) -> None:
    """After a failed refresh, keep an earlier generated copy from hiding user changes."""
    try:
        if not _is_session_owned(target, digest):
            _restore_user_link(target, source, digest)
    except OSError:
        _logger.exception("Could not relink the user's Pi MCP config into %s", target.parent)
