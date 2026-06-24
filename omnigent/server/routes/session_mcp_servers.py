"""Routes for session-scoped MCP server management."""

from __future__ import annotations

import asyncio
import gzip
import io
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Request, Response, status

from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import session_stream
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider, local_single_user_enabled
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.schemas import (
    MCPServerSummary,
    SessionAgentChangedEvent,
    UpsertMCPServerRequest,
)
from omnigent.spec import AgentSpec, extract_safe
from omnigent.spec.types import MCPServerConfig
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.permission_store import PermissionStore


@dataclass(frozen=True)
class _McpLocation:
    """Where an MCP server declaration lives inside a bundle."""

    source: Literal["file", "inline"]
    path: Path
    raw: dict[str, Any]


def create_session_mcp_servers_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    artifact_store: ArtifactStore | None,
    agent_cache: AgentCache | None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build routes under ``/sessions/{session_id}/agent/mcp-servers``."""

    router = APIRouter()

    async def _bound_agent(
        request: Request,
        session_id: str,
        required_level: int,
    ) -> Agent:
        """Authorize the caller and return the session's bound agent."""
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id,
            session_id,
            required_level,
            permission_store,
            conversation_store,
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if conv.agent_id is None:
            raise OmnigentError("Session has no agent binding", code=ErrorCode.INVALID_INPUT)
        agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent is None:
            raise OmnigentError("Agent not found", code=ErrorCode.NOT_FOUND)
        return agent

    @router.get("/sessions/{session_id}/agent/mcp-servers")
    async def list_mcp_servers(request: Request, session_id: str) -> dict[str, Any]:
        """List safe MCP server summaries for a session's bound agent."""
        agent = await _bound_agent(request, session_id, LEVEL_READ)
        if agent_cache is None:
            raise OmnigentError("Agent cache not configured", code=ErrorCode.INTERNAL_ERROR)
        loaded = await asyncio.to_thread(
            agent_cache.load,
            agent.id,
            agent.bundle_location,
            expand_env=agent.session_id is None,
        )
        return {
            "object": "list",
            "data": [
                _summary_from_config(server).model_dump() for server in loaded.spec.mcp_servers
            ],
        }

    @router.post("/sessions/{session_id}/agent/mcp-servers")
    async def create_mcp_server(
        request: Request,
        session_id: str,
        body: UpsertMCPServerRequest,
    ) -> MCPServerSummary:
        """Create one MCP server declaration on a session-scoped agent."""
        agent = await _editable_agent(request, session_id)
        spec = await asyncio.to_thread(
            _mutate_bundle,
            agent,
            body,
            mode="create",
            target_name=None,
        )
        _publish_agent_changed(session_id, agent)
        return _summary_from_spec(spec, body.name)

    @router.put("/sessions/{session_id}/agent/mcp-servers/{server_name}")
    async def update_mcp_server(
        request: Request,
        session_id: str,
        server_name: str,
        body: UpsertMCPServerRequest,
    ) -> MCPServerSummary:
        """Replace one MCP server declaration on a session-scoped agent."""
        agent = await _editable_agent(request, session_id)
        spec = await asyncio.to_thread(
            _mutate_bundle,
            agent,
            body,
            mode="update",
            target_name=server_name,
        )
        _publish_agent_changed(session_id, agent)
        return _summary_from_spec(spec, body.name)

    @router.delete(
        "/sessions/{session_id}/agent/mcp-servers/{server_name}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_mcp_server(
        request: Request,
        session_id: str,
        server_name: str,
    ) -> Response:
        """Delete one MCP server declaration from a session-scoped agent."""
        agent = await _editable_agent(request, session_id)
        await asyncio.to_thread(
            _mutate_bundle,
            agent,
            None,
            mode="delete",
            target_name=server_name,
        )
        _publish_agent_changed(session_id, agent)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def _editable_agent(request: Request, session_id: str) -> Agent:
        """Return an editable session-scoped agent."""
        agent = await _bound_agent(request, session_id, LEVEL_EDIT)
        if agent.session_id is None:
            raise OmnigentError(
                "Built-in agents are read-only through this endpoint.",
                code=ErrorCode.INVALID_INPUT,
            )
        if artifact_store is None or agent_cache is None:
            raise OmnigentError(
                "Agent bundle storage not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return agent

    def _mutate_bundle(
        agent: Agent,
        body: UpsertMCPServerRequest | None,
        *,
        mode: Literal["create", "update", "delete"],
        target_name: str | None,
    ) -> AgentSpec:
        """Edit the bundle, validate it, store it, and refresh cache."""
        assert artifact_store is not None
        assert agent_cache is not None

        bundle_bytes = artifact_store.get(agent.bundle_location)
        if bundle_bytes is None:
            raise OmnigentError("Agent bundle not found", code=ErrorCode.INTERNAL_ERROR)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "agent"
            extract_safe(bundle_bytes, root)

            current_spec = validate_agent_bundle(
                bundle_bytes,
                enforce_handler_allowlist=not local_single_user_enabled(),
            )
            current_names = {server.name for server in current_spec.mcp_servers}
            if mode == "create":
                assert body is not None
                if body.name in current_names:
                    raise OmnigentError(
                        f"MCP server {body.name!r} already exists",
                        code=ErrorCode.CONFLICT,
                    )
                _write_new_mcp_server(root, body)
            elif mode == "update":
                assert body is not None
                assert target_name is not None
                location = _find_mcp_location(root, target_name)
                if location is None:
                    raise OmnigentError("MCP server not found", code=ErrorCode.NOT_FOUND)
                if body.name != target_name and body.name in current_names:
                    raise OmnigentError(
                        f"MCP server {body.name!r} already exists",
                        code=ErrorCode.CONFLICT,
                    )
                _replace_mcp_server(location, target_name, body)
            else:
                assert target_name is not None
                location = _find_mcp_location(root, target_name)
                if location is None:
                    raise OmnigentError("MCP server not found", code=ErrorCode.NOT_FOUND)
                _delete_mcp_server(location, target_name)

            new_bundle = _tar_gz_dir(root)
            new_spec = validate_agent_bundle(
                new_bundle,
                enforce_handler_allowlist=not local_single_user_enabled(),
            )
            if new_spec.name != agent.name:
                raise OmnigentError(
                    "MCP edit changed the agent name; refusing to save.",
                    code=ErrorCode.INVALID_INPUT,
                )

        new_location = bundle_location(agent.id, new_bundle)
        if new_location != agent.bundle_location:
            artifact_store.put(new_location, new_bundle)
            updated = agent_store.update(agent.id, new_location)
            if updated is None:
                raise OmnigentError("Agent not found", code=ErrorCode.NOT_FOUND)
            agent_cache.replace(
                agent.id,
                new_location,
                new_bundle,
                expand_env=agent.session_id is None,
            )
        return new_spec

    return router


def _publish_agent_changed(session_id: str, agent: Agent) -> None:
    """Tell connected clients to refetch the session agent object."""
    event = SessionAgentChangedEvent(
        type="session.agent_changed",
        conversation_id=session_id,
        agent_id=agent.id,
        agent_name=agent.name,
    )
    session_stream.publish(session_id, event.model_dump())


def _summary_from_config(server: MCPServerConfig) -> MCPServerSummary:
    """Return the safe API summary for an MCP server config."""
    return MCPServerSummary(
        name=server.name,
        transport=server.transport,
        description=server.description,
        url=server.url,
        command=server.command,
        args=server.args,
    )


def _summary_from_spec(spec: AgentSpec, name: str) -> MCPServerSummary:
    """Find a saved server in a parsed spec and return its summary."""
    for server in spec.mcp_servers:
        if server.name == name:
            return _summary_from_config(server)
    raise OmnigentError("MCP server was not saved", code=ErrorCode.INTERNAL_ERROR)


def _write_new_mcp_server(root: Path, body: UpsertMCPServerRequest) -> None:
    """Create an MCP declaration in the bundle."""
    inline_path = _single_yaml_path(root)
    if inline_path is not None:
        _write_inline_server(inline_path, None, body, existing={})
        return

    mcp_dir = root / "tools" / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    path = mcp_dir / f"{body.name}.yaml"
    path.write_text(yaml.safe_dump(_body_to_file_yaml(body, {}), sort_keys=False))


def _replace_mcp_server(
    location: _McpLocation,
    target_name: str,
    body: UpsertMCPServerRequest,
) -> None:
    """Replace an existing MCP declaration."""
    if location.source == "file":
        next_path = location.path.with_name(f"{body.name}.yaml")
        next_path.write_text(
            yaml.safe_dump(_body_to_file_yaml(body, location.raw), sort_keys=False)
        )
        if next_path != location.path:
            location.path.unlink()
        return
    _write_inline_server(location.path, target_name, body, existing=location.raw)


def _delete_mcp_server(location: _McpLocation, target_name: str) -> None:
    """Remove an MCP declaration."""
    if location.source == "file":
        location.path.unlink()
        return

    config = _read_yaml_mapping(location.path)
    tools = config.get("tools")
    if isinstance(tools, dict):
        tools.pop(target_name, None)
    location.path.write_text(yaml.safe_dump(config, sort_keys=False))


def _find_mcp_location(root: Path, name: str) -> _McpLocation | None:
    """Find a server declaration by parsed MCP server name."""
    mcp_dir = root / "tools" / "mcp"
    if mcp_dir.is_dir():
        for path in sorted(mcp_dir.glob("*.yaml")):
            raw = _read_yaml_mapping(path)
            if str(raw.get("name")) == name:
                return _McpLocation(source="file", path=path, raw=raw)

    config_path = root / "config.yaml"
    if not config_path.exists():
        config_path = _single_yaml_path(root) or config_path
    if config_path.exists():
        config = _read_yaml_mapping(config_path)
        tools = config.get("tools")
        if isinstance(tools, dict):
            raw = tools.get(name)
            if isinstance(raw, dict) and str(raw.get("type", "")) == "mcp":
                return _McpLocation(source="inline", path=config_path, raw=dict(raw))
    return None


def _single_yaml_path(root: Path) -> Path | None:
    """Return the single-file agent YAML path when the bundle has one."""
    if (root / "config.yaml").exists():
        return None
    matches = [
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
    ]
    return matches[0] if len(matches) == 1 else None


def _write_inline_server(
    config_path: Path,
    old_name: str | None,
    body: UpsertMCPServerRequest,
    *,
    existing: dict[str, Any],
) -> None:
    """Write or move an inline ``tools.<name>.type: mcp`` block."""
    config = _read_yaml_mapping(config_path)
    tools = config.get("tools")
    if not isinstance(tools, dict):
        tools = {}
        config["tools"] = tools
    if old_name is not None and old_name != body.name:
        tools.pop(old_name, None)
    tools[body.name] = _body_to_inline_yaml(body, existing)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))


def _body_to_file_yaml(
    body: UpsertMCPServerRequest,
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Serialize a request body as ``tools/mcp/<name>.yaml``."""
    result: dict[str, Any] = {"name": body.name, "transport": body.transport}
    _copy_description(result, body)
    if body.transport == "http":
        result["url"] = body.url
        _preserve_keys(result, existing, ("headers", "auth", "timeout", "retry"))
    else:
        result["command"] = body.command
        if body.args:
            result["args"] = body.args
        _preserve_keys(result, existing, ("env", "timeout", "retry"))
    return result


def _body_to_inline_yaml(
    body: UpsertMCPServerRequest,
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Serialize a request body as an inline ``tools`` MCP block."""
    result: dict[str, Any] = {"type": "mcp"}
    _copy_description(result, body)
    if body.transport == "http":
        result["url"] = body.url
        _preserve_keys(result, existing, ("headers", "auth", "timeout", "retry"))
    else:
        result["command"] = body.command
        if body.args:
            result["args"] = body.args
        _preserve_keys(result, existing, ("env", "timeout", "retry"))
    return result


def _copy_description(result: dict[str, Any], body: UpsertMCPServerRequest) -> None:
    """Copy a non-empty description into a YAML mapping."""
    if body.description:
        result["description"] = body.description


def _preserve_keys(
    result: dict[str, Any],
    existing: dict[str, Any],
    keys: tuple[str, ...],
) -> None:
    """Preserve hidden or advanced config keys from the existing YAML."""
    for key in keys:
        if key in existing:
            result[key] = existing[key]


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML mapping from disk."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"YAML file must be a mapping: {path.name}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


def _normalize_tarinfo(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """Make tar members deterministic before content-addressing."""
    tarinfo.mtime = 0
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    tarinfo.mode = 0o755 if tarinfo.isdir() else 0o644
    return tarinfo


def _tar_gz_dir(bundle_dir: Path) -> bytes:
    """Pack a bundle directory into deterministic ``.tar.gz`` bytes."""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        tf.add(str(bundle_dir), arcname=".", filter=_normalize_tarinfo)
    return buf.getvalue()
