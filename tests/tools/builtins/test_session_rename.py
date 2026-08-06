"""Tests for the framework-owned current-session rename tool."""

from omnigent.tools.builtins.session_rename import SysSessionRenameTool


def test_session_rename_schema_is_self_scoped() -> None:
    schema = SysSessionRenameTool().get_schema()["function"]

    assert schema["name"] == "sys_session_rename"
    assert schema["parameters"]["required"] == ["title"]
    assert set(schema["parameters"]["properties"]) == {"title"}


def test_session_rename_schema_admits_a_structured_title() -> None:
    """
    The advertised bound must fit the structured titles the tool now
    permits. A 75-char ``repo::branch::date::role`` title is the shape
    operators asked for, and a 60-char bound silently forced them to
    drop a segment to fit.
    """
    schema = SysSessionRenameTool().get_schema()["function"]
    structured = "rpw-agent-marketplace::wave/2026-08-02-backlog::2026-08-04::wave_supervisor"

    assert len(structured) <= schema["parameters"]["properties"]["title"]["maxLength"]


def test_session_rename_description_permits_structured_titles() -> None:
    """
    The LLM-facing text must not read as forbidding the structured shape
    the tool now accepts, and must state the bound so a caller can plan
    a title that fits.
    """
    description = SysSessionRenameTool.description()

    assert "structured" in description
    assert "120" in description
