"""Bridge-local tools on the native Claude harness flag errors like relayed tools.

Relay-routed calls get ``isError`` from
``omnigent.runtime.mcp_tool_result.mcp_response_from_tool_result``. The
``sys_os_*`` tools the bridge serves itself outside an active turn returned the
same ``{"error": ...}`` / ``{"blocked": true}`` shapes with no flag at all.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import omnigent.harnesses.claude_native.bridge as bridge_mod
from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir


@pytest.fixture()
def isolated_bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temp_root = tmp_path.resolve()
    monkeypatch.setattr(bridge_mod, "_TRUSTED_PARENT", temp_root)
    monkeypatch.setattr(
        bridge_mod,
        "_BRIDGE_ROOT",
        temp_root / f"omnigent-{bridge_mod.stable_user_id()}" / "claude-native",
    )
    monkeypatch.setattr(bridge_mod, "post_tools_changed", lambda *a, **k: None)
    return temp_root


class _LocalTool:
    def __init__(self, output: str) -> None:
        self.output = output

    def invoke(self, arguments: str, ctx: object) -> str:
        del arguments, ctx
        return self.output


@pytest.mark.parametrize(
    ("output", "flagged"),
    [
        ('{"error": "Access blocked"}', True),
        ('{"blocked": true, "reason": "policy"}', True),
        ('{"error": ""}', False),
        ('{"stdout": "ok", "exit_code": 0}', False),
        ("[1, 2]", False),
        ("plain text", False),
    ],
)
def test_bridge_local_tool_errors_are_flagged(
    isolated_bridge_root: Path, output: str, flagged: bool
) -> None:
    bridge_dir = prepare_bridge_dir(
        f"conv_{uuid.uuid4().hex[:12]}", workspace=isolated_bridge_root
    )
    result = bridge_mod._call_mcp_tool(
        {"name": "local_tool", "arguments": {}},
        {"local_tool": _LocalTool(output)},  # type: ignore[dict-item]
        bridge_dir,
    )
    assert result["content"] == [{"type": "text", "text": output}]
    assert result.get("isError", False) is flagged


@pytest.mark.parametrize(
    ("command", "exit_code", "flagged"),
    [("printf ok", 0, False), ("printf 'no such thing' >&2; exit 3", 3, True)],
    ids=["exit-0", "exit-3"],
)
def test_real_shell_tool_nonzero_exit_is_flagged(
    isolated_bridge_root: Path, command: str, exit_code: int, flagged: bool
) -> None:
    """The real ``sys_os_shell`` tool, built the way the bridge MCP server builds it.

    This is an intentional consequence of flagging bridge-local errors: the shell
    tool reports a nonzero exit with a truthy top-level ``error``
    (``omnigent.inner.os_env``), so the bridge now marks that result ``isError``,
    the field Claude Code reads from the MCP result for the model's tool result.
    A zero exit carries no ``error`` and stays unflagged.
    """
    workspace = isolated_bridge_root / "ws"
    workspace.mkdir()
    bridge_dir = prepare_bridge_dir(f"conv_{uuid.uuid4().hex[:12]}", workspace=workspace)
    config = json.loads((bridge_dir / bridge_mod._CONFIG_FILE).read_text())
    tools, close_tools = bridge_mod._build_tools(config)
    try:
        assert "sys_os_shell" in tools
        response = bridge_mod._handle_mcp_request(
            "tools/call",
            {"name": "sys_os_shell", "arguments": {"command": command}},
            tools,
            bridge_dir,
        )
    finally:
        close_tools()
    payload = json.loads(response["content"][0]["text"])
    assert payload["exit_code"] == exit_code
    assert bool(payload.get("error")) is flagged
    assert response.get("isError", False) is flagged
