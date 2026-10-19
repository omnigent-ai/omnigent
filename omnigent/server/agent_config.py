"""Partial edits and credential-free projections of standard agent bundles."""

from __future__ import annotations

import hashlib
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.server.auth import local_single_user_enabled
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.routes.session_mcp_servers import (
    _find_mcp_location,
    _McpLocation,
    _tar_gz_dir,
    assert_mcp_server_request_safe,
)
from omnigent.server.schemas import (
    _MCP_SERVER_NAME_RE,
    AgentConfigObject,
    AgentConfigPatch,
    AgentMCPConfig,
    MCPServerPatch,
    UpsertMCPServerRequest,
)
from omnigent.spec import AgentSpec, ExtractionError, MCPServerConfig, extract_safe
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES
from omnigent.spec.parser import _ConfigYamlLoader


def _not_editable(message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "agent_config_not_editable", "message": message},
    )


def _merge_mcp(
    raw: dict[str, Any], patch: MCPServerPatch, server: MCPServerConfig | None
) -> dict[str, Any]:
    merged = dict(raw)
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        if field in {"headers", "env"}:
            values = dict(raw.get(field) or {})
            if value is None:
                values.clear()
            else:
                for key, item in value.items():
                    matching = [existing for existing in values if str(existing) == key]
                    if len(matching) > 1:
                        raise _not_editable("Ambiguous credential names cannot be edited")
                    target = matching[0] if matching else key
                    if item is None:
                        values.pop(target, None)
                    else:
                        values[target] = item
            if values:
                merged[field] = values
            elif raw.get(field):
                merged.pop(field, None)
        elif server is not None and value == getattr(server, field):
            continue
        elif value is None:
            merged.pop(field, None)
        else:
            merged[field] = value
    return merged


def _edit_mcp(
    root: Path, config: dict[str, Any], spec: AgentSpec, patches: dict[str, MCPServerPatch | None]
) -> set[str]:
    """Merge declarations in their original files; return changed server names."""
    servers = {server.name: server for server in spec.mcp_servers}
    changed: set[str] = set()
    for name, patch in patches.items():
        if not re.fullmatch(_MCP_SERVER_NAME_RE, name) or name in {".", ".."}:
            raise OmnigentError("Invalid MCP server name", code=ErrorCode.INVALID_INPUT)
        server = servers.get(name)
        location = _find_mcp_location(root, name)
        tools = config.get("tools") or {}
        matching = [key for key in tools if str(key) == name]
        if len(matching) > 1:
            raise _not_editable("Ambiguous MCP declarations cannot be edited")
        inline_key = matching[0] if matching else name
        inline = tools.get(inline_key)
        if isinstance(inline, dict) and inline.get("type") == "mcp":
            if location is not None and location.source == "file":
                raise _not_editable("Ambiguous MCP declarations cannot be edited")
            if location is None:
                location = _McpLocation("inline", root / "config.yaml", inline)
        if (server is not None and server.transport == "registry") or (
            location is not None and server is None
        ):
            raise _not_editable("Managed MCP declarations are read-only")
        if patch is None:
            if location is None:
                continue
            if location.source == "file":
                location.path.unlink()
            else:
                config["tools"] = dict(config["tools"])
                config["tools"].pop(inline_key)
            changed.add(name)
            continue

        transport = server.transport if server else patch.transport
        if transport not in {"http", "stdio"}:
            raise OmnigentError(
                "New MCP servers require a transport", code=ErrorCode.INVALID_INPUT
            )
        if "transport" in patch.model_fields_set and patch.transport != transport:
            raise OmnigentError(
                "MCP transport changes are not supported", code=ErrorCode.INVALID_INPUT
            )
        if location is not None:
            # The root parser keeps on/off as strings; sidecars use safe_load.
            raw = config["tools"][inline_key] if location.source == "inline" else location.raw
        else:
            raw = {"name": name, "transport": transport}
        merged = _merge_mcp(raw, patch, server)
        if location is not None and merged == raw:
            continue
        disallowed = (
            ("command", "args", "env", "sandbox") if transport == "http" else ("url", "headers")
        )
        if any(key in merged and key in patch.model_fields_set for key in disallowed):
            raise OmnigentError(
                "MCP fields do not match its transport", code=ErrorCode.INVALID_INPUT
            )
        if location is not None and location.source == "inline":
            config["tools"] = dict(config["tools"])
            config["tools"][inline_key] = merged
        else:
            path = location.path if location else root / "tools" / "mcp" / f"{name}.yaml"
            if location is None and path.exists():
                raise _not_editable("An existing bundle asset occupies the MCP declaration path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
        changed.add(name)
    return changed


def edit_agent_bundle(bundle: bytes, patch: AgentConfigPatch | None) -> tuple[bytes, AgentSpec]:
    """Read or patch a standard bundle without expanding environment references.

    Omitted fields and unrelated files remain intact. Edited YAML loses comments
    and formatting. Semantic no-ops retain the original archive bytes.
    """
    try:
        return _edit_agent_bundle(bundle, patch)
    except OmnigentError as exc:
        if exc.code in {ErrorCode.FORBIDDEN, ErrorCode.INTERNAL_ERROR}:
            raise
    except OSError:
        raise OmnigentError(
            "Agent configuration edit failed", code=ErrorCode.INTERNAL_ERROR
        ) from None
    except (ExtractionError, yaml.YAMLError, ValueError, TypeError, tarfile.TarError):
        pass
    # Loader and schema exceptions can contain credentials, including in chained tracebacks.
    raise OmnigentError("Invalid agent configuration", code=ErrorCode.INVALID_INPUT) from None


def _edit_agent_bundle(bundle: bytes, patch: AgentConfigPatch | None) -> tuple[bytes, AgentSpec]:
    enforce_handlers = not local_single_user_enabled()
    spec = validate_agent_bundle(bundle, enforce_handler_allowlist=enforce_handlers)
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "agent"
        extract_safe(bundle, root)
        config_path = root / "config.yaml"
        if not config_path.is_file():
            raise _not_editable(
                "Only standard config.yaml agent bundles support configuration edits"
            )
        if patch is None or not patch.model_fields_set:
            return bundle, spec
        config = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_ConfigYamlLoader)
        original_config = dict(config)
        fields = patch.model_fields_set

        if "description" in fields and patch.description != spec.description:
            config["description"] = patch.description
        if fields & {"model", "harness"}:
            if spec.executor.type != "omnigent":
                raise _not_editable("Model and harness edits require executor.type: omnigent")
            executor = dict(config.get("executor") or {})
            if "model" in fields and patch.model != spec.executor.model:
                executor["model"] = patch.model
            if "harness" in fields:
                harness = canonicalize_harness(patch.harness)
                if harness not in OMNIGENT_HARNESSES:
                    raise OmnigentError("Unsupported harness", code=ErrorCode.INVALID_INPUT)
                if patch.harness and patch.harness.startswith("acp:"):
                    if not re.fullmatch(r"acp:[a-z0-9]+(?:-[a-z0-9]+)*", patch.harness):
                        raise OmnigentError("Invalid ACP selection", code=ErrorCode.INVALID_INPUT)
                    harness = patch.harness
                current = spec.executor.config.get("harness")
                if harness != (
                    current if str(current).startswith("acp:") else canonicalize_harness(current)
                ):
                    executor["config"] = {**(executor.get("config") or {}), "harness": harness}
            if executor != (config.get("executor") or {}):
                config["executor"] = executor
        if "instructions" in fields:
            instructions = patch.instructions or ""
            if instructions != (spec.instructions or ""):
                if instructions:
                    digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
                    path = root / f"instructions-{digest}.md"
                    if path.exists() and (
                        not path.is_file() or path.read_bytes() != instructions.encode("utf-8")
                    ):
                        raise _not_editable(
                            "An existing bundle asset occupies the instruction path"
                        )
                    path.write_text(instructions, encoding="utf-8")
                    config["instructions"] = path.name
                else:
                    config["instructions"] = ""
        touched = _edit_mcp(root, config, spec, patch.mcp_servers or {})
        if config == original_config and not touched:
            return bundle, spec
        if config != original_config:
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        result = _tar_gz_dir(root, original_bundle=bundle)
        updated = validate_agent_bundle(result, enforce_handler_allowlist=enforce_handlers)
        if updated.name != spec.name or updated.executor.type != spec.executor.type:
            raise OmnigentError("Agent identity cannot change", code=ErrorCode.INVALID_INPUT)
        for server in updated.mcp_servers:
            if server.name in touched:
                assert_mcp_server_request_safe(
                    UpsertMCPServerRequest(
                        name=server.name,
                        transport=server.transport,
                        url=server.url,
                        command=server.command,
                        args=server.args,
                    )
                )
        return result, updated


def agent_config_object(agent: Agent, spec: AgentSpec) -> AgentConfigObject:
    """Project editable fields, exposing credential names without their values."""
    try:
        return _agent_config_object(agent, spec)
    except (ValueError, TypeError):
        raise OmnigentError("Invalid agent configuration", code=ErrorCode.INVALID_INPUT) from None


def _agent_config_object(agent: Agent, spec: AgentSpec) -> AgentConfigObject:
    return AgentConfigObject(
        id=agent.id,
        name=agent.name,
        version=agent.version,
        description=agent.description if agent.description is not None else spec.description,
        harness=spec.executor.harness_kind,
        model=spec.executor.model,
        instructions=spec.instructions,
        mcp_servers={
            server.name: AgentMCPConfig(
                transport=server.transport,
                description=server.description,
                url=server.url,
                command=server.command,
                args=server.args,
                configured_header_names=sorted(str(key) for key in (server.headers or {})),
                configured_env_names=sorted(str(key) for key in (server.env or {})),
                editable=server.transport != "registry",
            )
            for server in spec.mcp_servers
        },
    )
