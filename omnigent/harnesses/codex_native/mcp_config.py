"""Keep a native Codex session's private ``[mcp_servers]`` in step with the user's config.

A session's private ``CODEX_HOME/config.toml`` is copied from the user's Codex
home only at first launch, and Codex loads MCP servers only at process start, so
a server configured after that would never reach a resumed session. Every launch
therefore folds the user's current ``[mcp_servers.*]`` tables into the private
copy, three-way against the source tables recorded at the previous launch: a
server the user added, changed or removed since then follows the user's config,
and a server the user left alone keeps any session-local state.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import tomlkit
import tomllib
from tomlkit.exceptions import TOMLKitError

from omnigent.harnesses.codex_native.launch_args import (
    _write_private_config,
    codex_config_profile_update_pending,
)

_logger = logging.getLogger(__name__)

#: Sidecar holding the source ``[mcp_servers]`` tables as of the last sync.
MCP_SOURCE_STATE_FILENAME = ".omnigent-mcp-servers-source.toml"
#: Regenerated on every launch by ``_inject_mcp_server_config``; never synced.
_OMNIGENT_SERVER = "omnigent"


class CodexMcpInventoryError(RuntimeError):
    """The session's private Codex config lacks MCP servers the user's config declares."""


def sync_codex_home_mcp_servers(codex_home: Path, source_home: Path) -> frozenset[str] | None:
    """
    Fold the user's current ``[mcp_servers.*]`` into the private session config.

    For each server other than the generated ``omnigent`` relay, compared with
    the source tables recorded by the previous sync:

    - declared by the source and missing privately, or changed in the source:
      take the source table;
    - removed from the source: remove it privately;
    - otherwise: keep the private table (session-local edits and additions).

    A home with no recorded source (created before this sync existed) treats
    every source server as changed, which repairs a stale copy in place.
    Never modifies the source home, and never logs table values: they can
    carry credentials.

    Must run before ``materialize_codex_config_profile`` so the profile journal
    records these changes as private-layer edits and keeps the profile on top.

    :param codex_home: Private per-session ``CODEX_HOME``, e.g.
        ``~/.omnigent/codex-native/conv_abc123/codex-home``.
    :param source_home: The user's Codex home, e.g. ``~/.codex``.
    :returns: The source server names this launch must expose, e.g.
        ``frozenset({"github", "slack"})``; ``None`` when the sync was skipped
        and there is nothing to verify against.
    """
    config_path = codex_home / "config.toml"
    if not config_path.is_file() or config_path.is_symlink() or not source_home.is_dir():
        return None
    if codex_home.resolve() == source_home.resolve():
        return None
    source = _read_source_servers(source_home / "config.toml")
    if source is None:
        return None
    if codex_config_profile_update_pending(codex_home):
        # The profile step's crash recovery requires the config it last wrote;
        # sync on the next launch, once that update has completed.
        _logger.warning("Deferring native Codex MCP server sync: a profile update is pending")
        return None
    try:
        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, TOMLKitError):
        _logger.warning(
            "Could not sync native Codex MCP servers: invalid private config %s", config_path
        )
        return None
    state_path = codex_home / MCP_SOURCE_STATE_FILENAME
    previous = _read_recorded_source(state_path)
    recorded = previous or {}
    current = _servers_table(document.unwrap())
    merged = copy.deepcopy(current)
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    for name in sorted(source.keys() | recorded.keys()):
        if name in source:
            if name not in merged:
                added.append(name)
            elif source[name] != recorded.get(name) and merged[name] != source[name]:
                updated.append(name)
            else:
                continue
            merged[name] = copy.deepcopy(source[name])
        elif previous is not None and name in merged:
            removed.append(name)
            del merged[name]
    if merged != current:
        if "mcp_servers" in document:
            del document["mcp_servers"]
        if merged:
            document["mcp_servers"] = merged
        _write_private_config(config_path, tomlkit.dumps(document))
        _logger.info(
            "Synced native Codex MCP servers from %s: added=%s updated=%s removed=%s",
            source_home / "config.toml",
            added,
            updated,
            removed,
        )
    if source != previous:
        _write_private_config(state_path, tomlkit.dumps({"mcp_servers": source}))
    return frozenset(source)


def missing_codex_home_mcp_servers(codex_home: Path, expected: frozenset[str] | None) -> list[str]:
    """
    Name the expected MCP servers absent from the private session config.

    Run after every launch-time config write: anything listed here would be
    silently unavailable for the life of the Codex process. A server that is
    present but ``enabled = false`` is a deliberate choice, not a mismatch.

    :param codex_home: Private per-session ``CODEX_HOME``.
    :param expected: Names returned by :func:`sync_codex_home_mcp_servers`;
        ``None`` (sync skipped) checks nothing.
    :returns: Sorted missing names, e.g. ``["github", "slack"]``. Empty when
        the private config cannot be read (Codex then reports the config
        error itself).
    """
    if not expected:
        return []
    try:
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    present = _servers_table(config)
    return sorted(name for name in expected if name not in present)


def _servers_table(config: dict[str, Any]) -> dict[str, Any]:
    """Return the ``mcp_servers`` table of an unwrapped config, or ``{}``."""
    servers = config.get("mcp_servers")
    return servers if isinstance(servers, dict) else {}


def _read_source_servers(source_config: Path) -> dict[str, Any] | None:
    """
    Read the user's ``[mcp_servers.*]`` tables, minus the generated relay.

    :param source_config: The user's ``config.toml``.
    :returns: Server tables by name, or ``None`` when the file is missing,
        unreadable or unparsable, so the caller leaves the private copy as it
        is rather than act on a partial view (e.g. mid-save by an editor).
    """
    try:
        text = source_config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        _logger.warning("Could not read Codex config %s for MCP sync", source_config)
        return None
    try:
        servers = _servers_table(tomlkit.parse(text).unwrap())
    except TOMLKitError:
        _logger.warning("Could not parse Codex config %s for MCP sync", source_config)
        return None
    return {name: table for name, table in servers.items() if name != _OMNIGENT_SERVER}


def _read_recorded_source(state_path: Path) -> dict[str, Any] | None:
    """
    Read the source tables recorded by the previous sync.

    :param state_path: The private home's :data:`MCP_SOURCE_STATE_FILENAME`.
    :returns: Recorded tables by name, or ``None`` when nothing usable was
        recorded (the sync then treats every source server as changed).
    """
    try:
        state = tomllib.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        _logger.warning("Ignoring unreadable native Codex MCP sync state %s", state_path)
        return None
    servers = state.get("mcp_servers", {})
    return servers if isinstance(servers, dict) else None
