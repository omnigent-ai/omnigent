from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.entities.environment_filesystem import FilesystemPathNotFound, InvalidPath
from omnigent.runner.managed_artifacts import (
    artifact_spawn_env,
    discover_managed_artifacts,
    managed_artifact_dir,
    read_managed_artifact,
    with_artifact_spawn_env,
    write_managed_artifact_text,
)


def test_managed_artifact_dir_uses_configured_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    assert managed_artifact_dir("conv_abc123") == (
        tmp_path / "artifacts" / "sessions" / "conv_abc123"
    )
    assert artifact_spawn_env("conv_abc123") == {
        "OMNIGENT_ARTIFACT_DIR": str(tmp_path / "artifacts" / "sessions" / "conv_abc123")
    }
    assert with_artifact_spawn_env({"EXISTING": "1"}, "conv_abc123") == {
        "EXISTING": "1",
        "OMNIGENT_ARTIFACT_DIR": str(tmp_path / "artifacts" / "sessions" / "conv_abc123"),
    }


def test_discover_managed_artifacts_keeps_only_canonical_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    root = managed_artifact_dir("conv_discovery")
    (root / "overview.html").parent.mkdir(parents=True)
    (root / "overview.html").write_text("overview")
    (root / "revenue").mkdir()
    (root / "revenue" / "index.html").write_text("revenue")
    (root / "revenue" / "help.html").write_text("ignored")
    (root / "nested" / "deeper").mkdir(parents=True)
    (root / "nested" / "deeper" / "index.html").write_text("ignored")

    entries = discover_managed_artifacts("conv_discovery")

    assert [entry.path for entry in entries] == [
        "artifacts/overview.html",
        "artifacts/revenue/index.html",
    ]
    assert [entry.name for entry in entries] == ["overview.html", "index.html"]


@pytest.mark.asyncio
async def test_read_managed_artifact_is_confined_and_does_not_follow_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    root = managed_artifact_dir("conv_read")
    artifact = root / "revenue"
    artifact.mkdir(parents=True)
    (artifact / "index.html").write_text("<h1>Revenue</h1>")
    (artifact / "app.js").write_text("console.log('ok')")
    outside = tmp_path / "outside.js"
    outside.write_text("secret")
    (artifact / "escape.js").symlink_to(outside)

    content = await read_managed_artifact(
        "conv_read",
        "artifacts/revenue/app.js",
        artifact_root="artifacts/revenue",
    )

    assert content.data == b"console.log('ok')"
    with pytest.raises(InvalidPath):
        await read_managed_artifact(
            "conv_read",
            "artifacts/other/app.js",
            artifact_root="artifacts/revenue",
        )
    with pytest.raises(FilesystemPathNotFound):
        await read_managed_artifact(
            "conv_read",
            "artifacts/revenue/escape.js",
            artifact_root="artifacts/revenue",
        )


@pytest.mark.asyncio
async def test_runner_mcp_manager_publishes_with_session_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.runner.managed_artifacts import PUBLISH_DESIGN_ARTIFACT_TOOL
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.spec.types import AgentSpec

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session_id = "conv_publish"
    await write_managed_artifact_text(
        session_id,
        "artifacts/revenue/index.html",
        "<h1>Revenue</h1>",
    )
    await write_managed_artifact_text(
        session_id,
        "artifacts/revenue/app.js",
        "console.log('ok')",
    )

    manager = RunnerMcpManager()
    output = await manager.call_tool(
        AgentSpec(spec_version=1, name="willy"),
        PUBLISH_DESIGN_ARTIFACT_TOOL,
        {
            "entry_path": "artifacts/revenue/index.html",
            "title": "Revenue",
            "operation": "created",
        },
        session_id=session_id,
    )

    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["artifact_root"] == "artifacts/revenue"
    assert payload["resource_count"] == 2


@pytest.mark.asyncio
async def test_execute_tool_publishes_managed_artifact_before_local_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.runner import tool_dispatch
    from omnigent.runner.managed_artifacts import PUBLISH_DESIGN_ARTIFACT_TOOL

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session_id = "conv_dispatch_publish"
    await write_managed_artifact_text(
        session_id,
        "artifacts/revenue/index.html",
        "<h1>Revenue</h1>",
    )
    await write_managed_artifact_text(
        session_id,
        "artifacts/revenue/app.js",
        "console.log('ok')",
    )

    monkeypatch.setattr(
        tool_dispatch,
        "_is_spec_local_python_tool",
        lambda _tool_name, _agent_spec: True,
    )

    async def fail_local_python(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("OMNIGENT_ARTIFACT_DIR is not configured")

    monkeypatch.setattr(tool_dispatch, "_execute_local_python_tool", fail_local_python)

    output = await tool_dispatch.execute_tool(
        tool_name=PUBLISH_DESIGN_ARTIFACT_TOOL,
        arguments=json.dumps(
            {
                "entry_path": "artifacts/revenue/index.html",
                "title": "Revenue",
                "operation": "created",
            }
        ),
        agent_spec=object(),
        conversation_id=session_id,
    )

    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["artifact_root"] == "artifacts/revenue"
    assert payload["resource_count"] == 2
