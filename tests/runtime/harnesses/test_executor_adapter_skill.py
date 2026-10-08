"""Skill-name extraction for skill-execution telemetry (executor adapter)."""

from __future__ import annotations

from omnigent.runtime.harnesses._executor_adapter import (
    _SKILL_TOOL_NAMES,
    _extract_skill_name,
    _strip_mcp_tool_prefix,
)


def test_skill_tool_names_cover_native_and_builtin() -> None:
    """Both Claude Code's native ``Skill`` and Omnigent's ``load_skill`` count."""
    assert "Skill" in _SKILL_TOOL_NAMES
    assert "load_skill" in _SKILL_TOOL_NAMES
    # load_skill arrives MCP-prefixed under the claude-sdk harness.
    assert _strip_mcp_tool_prefix("mcp__omnigent__load_skill") == "load_skill"


def test_extract_skill_name_load_skill_name_key() -> None:
    assert _extract_skill_name("load_skill", {"name": "code-review"}) == "code-review"


def test_extract_skill_name_native_command_key() -> None:
    # Claude Code's native Skill tool is commonly {"command": "<skill>"}.
    assert _extract_skill_name("Skill", {"command": "cardinal"}) == "cardinal"


def test_extract_skill_name_falls_back_to_first_string() -> None:
    # Tolerant of an unexpected key so a schema change doesn't silently drop it.
    assert _extract_skill_name("Skill", {"unexpected": "creditflow"}) == "creditflow"


def test_extract_skill_name_strips_whitespace() -> None:
    assert _extract_skill_name("load_skill", {"name": "  imaforge  "}) == "imaforge"


def test_extract_skill_name_none_when_no_usable_value() -> None:
    assert _extract_skill_name("Skill", {}) is None
    assert _extract_skill_name("Skill", {"n": 5}) is None
    assert _extract_skill_name("Skill", None) is None  # type: ignore[arg-type]
