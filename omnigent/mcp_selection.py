"""Session MCP selections, kept separately from downloadable agent source."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec.types import AgentSpec, MCPServerConfig

MCP_SELECTION_PREFIX = "omnigent.mcp_service."
MCP_SELECTION_HEADER = "X-Omnigent-MCP-Selection"


def mcp_selection_revision(bundle_location: str, labels: dict[str, str]) -> str:
    """Invalidate session caches after selection edits, even if a reset was missed."""
    selections = sorted((k, v) for k, v in labels.items() if k.startswith(MCP_SELECTION_PREFIX))
    if not selections:
        return bundle_location
    digest = hashlib.sha256(json.dumps(selections).encode()).hexdigest()[:16]
    return f"{bundle_location}:mcp:{digest}"


def selected_mcp_spec(spec: AgentSpec, labels: dict[str, str]) -> AgentSpec:
    """Apply per-service overrides without changing the shared cached spec."""
    if not any(key.startswith(MCP_SELECTION_PREFIX) for key in labels):
        return spec
    servers = {server.name: server for server in spec.mcp_servers}
    for key, value in labels.items():
        if not key.startswith(MCP_SELECTION_PREFIX):
            continue
        name = key.removeprefix(MCP_SELECTION_PREFIX)
        if value not in {"0", "1"}:
            raise OmnigentError("Invalid session MCP selection", code=ErrorCode.INVALID_INPUT)
        existing = servers.get(name)
        if existing is not None and existing.transport != "registry":
            if value == "0":
                continue
            raise OmnigentError(
                f"MCP server {name!r} already uses a custom transport", code=ErrorCode.CONFLICT
            )
        if value == "0":
            servers.pop(name, None)
        elif existing is None:
            servers[name] = MCPServerConfig(name=name, transport="registry")
    return replace(spec, mcp_servers=list(servers.values()))


def registry_selection_labels(spec: AgentSpec, services: list[str]) -> dict[str, str]:
    """Keep each selection within the existing label key/value size limits."""
    labels = {MCP_SELECTION_PREFIX + name: "1" for name in services}
    selected_mcp_spec(spec, labels)
    return labels
