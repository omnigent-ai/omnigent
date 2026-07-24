from pathlib import Path

from omnigent.claude_native_bridge import _call_mcp_tool, _combined_mcp_tool_schemas


def test_manifest_advertises_and_loads_skill(tmp_path: Path) -> None:
    (tmp_path / "skill_registry.json").write_text(
        '{"relay-skill":{"content":"relay content","description":"relay"}}'
    )
    schemas = _combined_mcp_tool_schemas({}, tmp_path)
    assert [schema["name"] for schema in schemas] == ["load_skill"]
    result = _call_mcp_tool(
        {"name": "load_skill", "arguments": {"name": "relay-skill"}},
        {},
        tmp_path,
    )
    assert result == {"content": [{"type": "text", "text": "relay content"}]}
