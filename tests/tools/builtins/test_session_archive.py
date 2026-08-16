"""Unit tests for :mod:`omnigent.tools.builtins.session_archive`."""

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
    # The load-bearing safety clause: without it, nothing in the schema
    # stops the model from archiving a session a person is still using.
    assert "person is still working" in description
    # The unattended/finished-work framing that scopes when to call it.
    assert "unattended" in description
    # Scopes the tool away from sub-agent runs, which retire via close.
    assert "top-level" in description
    # Reversibility is the claim that makes always-on registration defensible.
    assert "unarchive" in description
