"""Uploaded tools must be rejected before importing their Python targets."""

from __future__ import annotations

import io
import json
import math
import sys
import tarfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.loader import load_agent_def
from omnigent.inner.tools import AgentTool, CancellableFunctionTool, FunctionTool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.bundles import validate_agent_bundle
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TOOL_TARGETS = (
    ("function", "callable"),
    (None, "callable"),
    ("cancellable_function", "runner"),
)
_REJECTION = (
    "Tool 'probe': uploaded agent bundles may not declare a server-side Python callable tool."
)


def _agent_with_tool(tool: dict[str, object], depth: int) -> dict[str, object]:
    tools: dict[str, object] = {"probe": tool}
    # Sub-agent names must be unique across the entire spec tree.
    for level in reversed(range(depth)):
        tools = {f"agent_{level}": {"type": "agent", "tools": tools}}
    return {
        "name": "test_agent",
        "prompt": "hello",
        "executor": {"harness": "claude-sdk"},
        "tools": tools,
    }


def _bundle(config: dict[str, object]) -> bytes:
    content = json.dumps(config).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("omnigent.yaml")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize("tool_type,target_field", _TOOL_TARGETS)
@pytest.mark.parametrize("target", ["math.sqrt", "missing_tool_target.target"])
@pytest.mark.parametrize("upload", [False, True], ids=["loader", "bundle"])
def test_untrusted_tools_rejected_before_import(
    depth: int, tool_type: str | None, target_field: str, target: str, upload: bool
) -> None:
    tool: dict[str, object] = {target_field: target}
    if tool_type is not None:
        tool["type"] = tool_type
    config = _agent_with_tool(tool, depth)
    bundle = _bundle(config)

    # Importing is the external side effect under test: a later validation
    # failure cannot undo module initialization in the shared server process.
    with mock.patch(
        "omnigent.inner.loader.importlib.import_module",
        side_effect=AssertionError("untrusted tool attempted an import"),
    ) as import_module:
        if upload:
            with pytest.raises(OmnigentError) as upload_error:
                validate_agent_bundle(bundle)
            assert upload_error.value.code == ErrorCode.INVALID_INPUT
            assert str(upload_error.value) == f"invalid agent bundle: {_REJECTION}"
        else:
            with pytest.raises(ValueError) as load_error:
                load_agent_def(config, enforce_handler_allowlist=True)
            assert str(load_error.value) == _REJECTION
        import_module.assert_not_called()


@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize("tool_type,target_field", _TOOL_TARGETS)
def test_trusted_loader_resolves_dynamic_tools(
    depth: int, tool_type: str | None, target_field: str
) -> None:
    tool: dict[str, object] = {target_field: "math.sqrt"}
    if tool_type is not None:
        tool["type"] = tool_type
    agent = load_agent_def(_agent_with_tool(tool, depth))
    tools = agent.tools
    for level in range(depth):
        parsed = tools[f"agent_{level}"]
        assert isinstance(parsed, AgentTool)
        tools = parsed.tools
    parsed = tools["probe"]
    if target_field == "runner":
        assert isinstance(parsed, CancellableFunctionTool)
        assert parsed.runner is math.sqrt
    else:
        assert isinstance(parsed, FunctionTool)
        assert parsed.callable is math.sqrt


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_trusted_bundle_accepts_dynamic_tools(depth: int) -> None:
    spec = validate_agent_bundle(
        _bundle(_agent_with_tool({"callable": "math.sqrt"}, depth)),
        enforce_handler_allowlist=False,
    )
    for _ in range(depth):
        assert len(spec.sub_agents) == 1
        spec = spec.sub_agents[0]
    assert len(spec.local_tools) == 1
    assert spec.local_tools[0].path == "math.sqrt"


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_untrusted_bundle_accepts_client_tools_without_dynamic_targets(depth: int) -> None:
    tool: dict[str, object] = {
        "type": "function",
        "runtime": "client",
        "parameters": {"type": "object", "properties": {}},
    }
    spec = validate_agent_bundle(_bundle(_agent_with_tool(tool, depth)))
    for _ in range(depth):
        assert len(spec.sub_agents) == 1
        spec = spec.sub_agents[0]
    assert len(spec.local_tools) == 1
    assert spec.local_tools[0].path is None


@pytest.fixture
def upload_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_local: bool,
) -> FastAPI:
    """Use the real deployment-mode flag and header auth with isolated stores."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1" if trusted_local else "0")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest.fixture
async def upload_client(upload_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Exercise multipart routes through the in-process HTTP transport."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upload_app),
        base_url="http://test",
        headers={"X-Forwarded-Email": "owner@example.com"},
    ) as client:
        yield client


@pytest.fixture
def import_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    """A real module whose initialization records whether validation imported it."""
    module_name = "uploaded_tool_import_probe"
    marker = tmp_path / "imported"
    (tmp_path / f"{module_name}.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "from math import sqrt as target\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield f"{module_name}.target", marker
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("trusted_local", [False, True], ids=["multi-user", "trusted-local"])
@pytest.mark.parametrize("operation", ["create", "replace"])
@pytest.mark.parametrize("depth", [0, 3])
@pytest.mark.parametrize("tool_type,target_field", _TOOL_TARGETS)
async def test_upload_routes_guard_imports(
    upload_client: httpx.AsyncClient,
    import_probe: tuple[str, Path],
    trusted_local: bool,
    operation: str,
    depth: int,
    tool_type: str | None,
    target_field: str,
) -> None:
    target, marker = import_probe
    tool: dict[str, object] = {target_field: target}
    if tool_type is not None:
        tool["type"] = tool_type
    bundle = _bundle(_agent_with_tool(tool, depth))
    files = {"bundle": ("agent.tar.gz", bundle, "application/gzip")}
    if operation == "create":
        response = await upload_client.post("/v1/sessions", data={"metadata": "{}"}, files=files)
        success_status = 201
    else:
        clean = _agent_with_tool({}, 0)
        clean["tools"] = {}
        created = await upload_client.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={"bundle": ("agent.tar.gz", _bundle(clean), "application/gzip")},
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        response = await upload_client.put(f"/v1/sessions/{session_id}/agent", files=files)
        success_status = 200

    if trusted_local:
        assert marker.exists(), "trusted uploads must retain dynamic tool loading"
        if tool_type == "cancellable_function":
            # The loader supports these, but spec translation has retired them.
            assert response.status_code == 400, response.text
            assert "was retired" in response.text
        else:
            assert response.status_code == success_status, response.text
    else:
        assert not marker.exists(), "untrusted validation imported the tool's module"
        assert response.status_code == 400, response.text
        assert _REJECTION in response.text
