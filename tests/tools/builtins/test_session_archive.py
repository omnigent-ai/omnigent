"""Unit tests for the ``sys_session_archive`` builtin schema."""

from __future__ import annotations

from omnigent.tools.builtins.session_archive import SysSessionArchiveTool


def test_schema_shape() -> None:
    """The tool advertises itself with no arguments."""
    schema = SysSessionArchiveTool().get_schema()["function"]

    assert schema["name"] == "sys_session_archive"
    assert schema["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_description_scopes_the_tool_to_finished_unattended_runs() -> None:
    """The description must steer the model away from live user sessions."""
    description = SysSessionArchiveTool.description()

    assert "current session" in description
    assert "unarchive" in description
