"""Server-wide default MCP servers — merge + wire (de)serialization (#4).

The shared MCP registry lets an operator declare MCP servers once in the server
``--config`` YAML (``mcp_servers:`` → :attr:`RuntimeCaps.default_mcp_servers`)
and have them offered to every session in addition to each agent's own.

MCP servers are *connected on the runner* (the MCP proxy delegates
``tools/list`` / ``tools/call`` to the runner's ``/mcp/execute``, which keys live
connections by :func:`compute_spec_hash` over ``spec.mcp_servers``). The runner
is a separate process without the server's config, so the defaults travel **over
the proxy call**: the server serializes them into the ``params`` and the runner
merges them into the session spec before connecting. Merging identically on both
``tools/list`` and ``tools/call`` keeps the spec hash — and therefore the cached
connection set — consistent across the two.

The ``retry`` field is intentionally dropped on the wire (default MCP servers
inherit ``tools.retry`` / SDK defaults); every other field round-trips.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from omnigent.spec.types import AgentSpec, MCPServerConfig

# Fields carried over the proxy wire. ``retry`` (a nested dataclass) is omitted
# by design — defaults inherit the agent's / SDK retry behaviour.
_WIRE_FIELDS = (
    "name",
    "transport",
    "url",
    "headers",
    "databricks_profile",
    "command",
    "args",
    "env",
    "tools",
    "description",
    "timeout",
)
# Collection fields that are meaningless when empty — skipped on rebuild so the
# MCPServerConfig transport validator never sees an empty wrong-transport field.
_COLLECTION_FIELDS = frozenset({"headers", "args", "env", "tools"})


def serialize_mcp_servers(servers: list[MCPServerConfig]) -> list[dict[str, Any]]:
    """Serialize MCP server configs to JSON-safe dicts for the proxy wire.

    :param servers: The configs to serialize (e.g. the server-wide defaults).
    :returns: One plain dict per server, carrying :data:`_WIRE_FIELDS`.
    """
    return [
        {
            "name": s.name,
            "transport": s.transport,
            "url": s.url,
            "headers": dict(s.headers),
            "databricks_profile": s.databricks_profile,
            "command": s.command,
            "args": list(s.args),
            "env": dict(s.env),
            "tools": list(s.tools) if s.tools is not None else None,
            "description": s.description,
            "timeout": s.timeout,
        }
        for s in servers
    ]


def deserialize_mcp_servers(raw: list[dict[str, Any]] | None) -> list[MCPServerConfig]:
    """Rebuild MCP server configs from their wire dicts.

    Only set, non-empty fields are passed to the constructor so the transport
    validator never trips on an empty wrong-transport collection (e.g. an
    ``headers={}`` on a stdio server).

    :param raw: The wire dicts from :func:`serialize_mcp_servers`, or ``None``.
    :returns: Reconstructed configs (``retry`` defaults to ``None``).
    """
    servers: list[MCPServerConfig] = []
    for d in raw or []:
        kwargs: dict[str, Any] = {}
        for key in _WIRE_FIELDS:
            value = d.get(key)
            if value is None:
                continue
            if key in _COLLECTION_FIELDS and not value:
                continue
            kwargs[key] = value
        servers.append(MCPServerConfig(**kwargs))
    return servers


def merge_default_mcp_servers(
    spec: AgentSpec,
    defaults: list[MCPServerConfig],
) -> AgentSpec:
    """Return a spec whose ``mcp_servers`` include the server-wide defaults.

    Defaults are appended, deduped by ``name`` — a server the agent/session
    already declares with that name wins (the default is dropped). The input
    spec is never mutated: when there is anything to add a shallow copy is
    returned via :func:`dataclasses.replace` (the agent cache hands out shared
    spec objects, so in-place mutation would leak across sessions). When there
    is nothing to add the original spec is returned unchanged.

    :param spec: The session's resolved agent spec.
    :param defaults: Server-wide default MCP servers (may be empty).
    :returns: ``spec`` unchanged, or a copy with the deduped defaults appended.
    """
    if not defaults:
        return spec
    existing = {s.name for s in (spec.mcp_servers or [])}
    additions = [d for d in defaults if d.name not in existing]
    if not additions:
        return spec
    return dataclasses.replace(spec, mcp_servers=list(spec.mcp_servers or []) + additions)
