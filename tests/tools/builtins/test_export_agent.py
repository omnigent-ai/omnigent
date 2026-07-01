"""Unit tests for :mod:`omnigent.tools.builtins.export_agent`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.export_agent import ExportAgentTool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with a sample agent directory."""
    agent_dir = tmp_path / "workspace" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text("name: my-agent\n")
    (agent_dir / "prompt.md").write_text("# Prompt\n")
    return tmp_path / "workspace"


@pytest.fixture()
def tool_ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        task_id="task_test",
        agent_id="agent_test",
        workspace=workspace,
    )


# ── Schema ───────────────────────────────────────────────


def test_schema_shape() -> None:
    """Schema has source and target as required string params."""
    tool = ExportAgentTool()
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "export_agent"
    assert set(func["parameters"]["required"]) == {"source", "target"}
    props = func["parameters"]["properties"]
    assert props["source"]["type"] == "string"
    assert props["target"]["type"] == "string"


def test_name_and_description() -> None:
    assert ExportAgentTool.name() == "export_agent"
    assert len(ExportAgentTool.description()) > 0


# ── Invoke: success ──────────────────────────────────────


def test_invoke_copies_directory(
    tool_ctx: ToolContext,
    tmp_path: Path,
) -> None:
    """invoke() copies the agent directory to the target path."""
    target = tmp_path / "export-target" / "my-agent"
    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "my-agent", "target": str(target)}),
        tool_ctx,
    )
    assert "Exported agent to" in result
    assert (target / "config.yaml").exists()
    assert (target / "prompt.md").exists()


def test_invoke_refuses_existing_target(
    tool_ctx: ToolContext,
    tmp_path: Path,
) -> None:
    """An existing target is refused and never deleted (no rmtree).

    Regression test for arbitrary directory deletion: the old code
    called ``shutil.rmtree`` on the LLM-controlled absolute target,
    which could wipe any directory on the user's filesystem.
    """
    target = tmp_path / "precious"
    target.mkdir()
    (target / "important.txt").write_text("do not delete")

    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "my-agent", "target": str(target)}),
        tool_ctx,
    )
    assert "Error" in result and "already exists" in result.lower()
    # The pre-existing directory and its contents are untouched.
    assert (target / "important.txt").read_text() == "do not delete"
    assert not (target / "config.yaml").exists()


# ── Invoke: error cases ──────────────────────────────────


def test_invoke_missing_source(tool_ctx: ToolContext) -> None:
    """Error when source is empty."""
    tool = ExportAgentTool()
    result = tool.invoke(json.dumps({"target": "/tmp/out"}), tool_ctx)
    assert "Error" in result and "source" in result.lower()


def test_invoke_missing_target(tool_ctx: ToolContext) -> None:
    """Error when target is empty."""
    tool = ExportAgentTool()
    result = tool.invoke(json.dumps({"source": "my-agent"}), tool_ctx)
    assert "Error" in result and "target" in result.lower()


def test_invoke_source_not_found(
    tool_ctx: ToolContext,
    tmp_path: Path,
) -> None:
    """Error when source directory doesn't exist in workspace."""
    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "nonexistent", "target": str(tmp_path / "out")}),
        tool_ctx,
    )
    assert "Error" in result and "not found" in result.lower()


def test_invoke_source_is_file(
    tool_ctx: ToolContext,
    tmp_path: Path,
) -> None:
    """Error when source is a file, not a directory."""
    assert tool_ctx.workspace is not None
    (tool_ctx.workspace / "just-a-file.txt").write_text("not a dir")
    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "just-a-file.txt", "target": str(tmp_path / "out")}),
        tool_ctx,
    )
    assert "Error" in result and "not a directory" in result.lower()


def test_invoke_no_workspace() -> None:
    """Error when workspace is None."""
    ctx = ToolContext(task_id="t", agent_id="a", workspace=None)
    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "my-agent", "target": "/tmp/out"}),
        ctx,
    )
    assert "Error" in result and "workspace" in result.lower()


# ── Invoke: containment / exfiltration ───────────────────


def test_invoke_rejects_source_escaping_workspace(
    tool_ctx: ToolContext,
    tmp_path: Path,
) -> None:
    """A traversal source path is rejected and nothing is copied."""
    target = tmp_path / "escape-out"
    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "../../etc", "target": str(target)}),
        tool_ctx,
    )
    assert "Error" in result and "escapes the workspace" in result.lower()
    assert not target.exists()


def test_invoke_does_not_dereference_source_symlink(
    tool_ctx: ToolContext,
    tmp_path: Path,
) -> None:
    """A symlink inside source is copied as a link, not dereferenced.

    Regression test for host-file exfiltration: copytree's default
    symlink dereference would copy the *contents* of the symlink's
    target (a host secret) into the exported directory.
    """
    assert tool_ctx.workspace is not None
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET")
    (tool_ctx.workspace / "my-agent" / "leak").symlink_to(secret)

    target = tmp_path / "export-symlink" / "my-agent"
    tool = ExportAgentTool()
    result = tool.invoke(
        json.dumps({"source": "my-agent", "target": str(target)}),
        tool_ctx,
    )
    assert "Exported agent to" in result

    exported_link = target / "leak"
    # The entry is preserved as a symlink rather than dereferenced
    # into a regular file holding the secret bytes.
    assert exported_link.is_symlink()
    # Removing the host file leaves the export dangling, proving the
    # secret's contents were never copied out of the workspace.
    secret.unlink()
    assert not exported_link.exists()
