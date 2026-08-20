"""Tests for the framework-owned current-session rename tool."""

from omnigent.tools.builtins.session_rename import SysSessionRenameTool


def test_session_rename_schema_is_self_scoped() -> None:
    schema = SysSessionRenameTool().get_schema()["function"]

    assert schema["name"] == "sys_session_rename"
    assert schema["parameters"]["required"] == ["title"]
    assert set(schema["parameters"]["properties"]) == {"title"}
