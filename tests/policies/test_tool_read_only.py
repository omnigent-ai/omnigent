"""Tool read-only declarations reach TOOL_CALL policy events.

Covers the YAML surface (function ``read_only``, MCP ``trust_annotations`` /
``read_only_tools``), the resolution precedence, and the single helper every
TOOL_CALL ``EvaluationContext`` is built through.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.inner.loader import _parse_tool
from omnigent.inner.tools import FunctionTool, MCPTool
from omnigent.policies.tool_read_only import (
    mcp_read_only_hint,
    resolve_tool_read_only,
    tool_call_context,
)
from omnigent.spec.omnigent import (
    _mcp_server_to_mcp_tool,
    _translate_function_tool_from_def,
    _translate_mcp_tool_from_def,
)
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, LocalToolInfo, MCPServerConfig, Phase

_REPO = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, tools: dict) -> Path:
    config = {"spec_version": 1, "name": "ro-agent", "tools": tools}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


def _load(name: str, data: dict) -> object:
    return _parse_tool(name, data, expand_env=False, allow_dynamic_tools=True)


# ── bundle config.yaml (spec_version: 1) ────────────────────────────────


def test_mcp_trust_annotations_and_read_only_tools_parsed(tmp_path: Path) -> None:
    spec = parse(
        _write(
            tmp_path,
            {
                "github": {
                    "type": "mcp",
                    "url": "http://github/mcp",
                    "trust_annotations": True,
                    "read_only_tools": ["list_issues"],
                },
                "other": {"type": "mcp", "url": "http://other/mcp"},
            },
        )
    )
    by_name = {s.name: s for s in spec.mcp_servers}
    assert by_name["github"].trust_annotations is True
    assert by_name["github"].read_only_tools == ["list_issues"]
    assert by_name["other"].trust_annotations is False
    assert by_name["other"].read_only_tools is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("trust_annotations", "true"), ("read_only_tools", "list_issues"), ("read_only_tools", [1])],
)
def test_mcp_read_only_fields_wrong_type_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(OmnigentError, match=field):
        parse(_write(tmp_path, {"github": {"type": "mcp", "url": "http://g/mcp", field: value}}))


@pytest.mark.parametrize(
    ("field", "value"), [("trust_annotations", True), ("read_only_tools", ["list_issues"])]
)
def test_standalone_mcp_file_rejects_read_only_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    """``tools/mcp/*.yaml`` doesn't support the declaration: fail loud, not silently drop."""
    _write(tmp_path, {})
    mcp_dir = tmp_path / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "github.yaml").write_text(
        yaml.dump({"name": "github", "transport": "http", "url": "http://g/mcp", field: value})
    )
    with pytest.raises(OmnigentError, match=field):
        parse(tmp_path)


# ── agent YAML tools (inner loader and its translation to the spec) ─────


@pytest.mark.parametrize("declared", [True, False, None])
def test_function_tool_read_only_loaded_and_translated(declared: bool | None) -> None:
    data: dict[str, object] = {"type": "function", "runtime": "client", "parameters": {}}
    if declared is not None:
        data["read_only"] = declared
    tool = _load("reader", data)
    assert isinstance(tool, FunctionTool)
    assert tool.read_only is declared
    assert _translate_function_tool_from_def("reader", tool).read_only is declared


def test_function_tool_read_only_wrong_type_rejected() -> None:
    with pytest.raises(ValueError, match="read_only"):
        _load("reader", {"type": "function", "runtime": "client", "read_only": "yes"})


def test_mcp_tool_read_only_fields_loaded_and_translated_both_ways() -> None:
    tool = _load(
        "github",
        {
            "type": "mcp",
            "url": "http://github/mcp",
            "trust_annotations": True,
            "read_only_tools": ["list_issues"],
        },
    )
    assert isinstance(tool, MCPTool)
    config = _translate_mcp_tool_from_def("github", tool)
    assert config.trust_annotations is True
    assert config.read_only_tools == ["list_issues"]
    back = _mcp_server_to_mcp_tool(config)
    assert back.trust_annotations is True
    assert back.read_only_tools == ["list_issues"]


def test_mcp_tool_without_declaration_translates_to_untrusted() -> None:
    tool = _load("github", {"type": "mcp", "command": "npx"})
    assert isinstance(tool, MCPTool)
    config = _translate_mcp_tool_from_def("github", tool)
    assert config.trust_annotations is False
    assert config.read_only_tools is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("trust_annotations", "true"), ("read_only_tools", "list_issues"), ("read_only_tools", [1])],
)
def test_mcp_tool_read_only_fields_wrong_type_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _load("github", {"type": "mcp", "url": "http://g/mcp", field: value})


# ── resolution ──────────────────────────────────────────────────────────


def _spec(*, trust: bool = False, ro_tools: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="ro-agent",
        local_tools=[
            LocalToolInfo(name="reader", path="m.reader", language="python", read_only=True),
            LocalToolInfo(name="writer", path="m.writer", language="python", read_only=False),
            LocalToolInfo(name="plain", path="m.plain", language="python"),
        ],
        mcp_servers=[
            MCPServerConfig(
                name="github",
                url="http://github/mcp",
                trust_annotations=trust,
                read_only_tools=ro_tools,
            )
        ],
    )


def test_function_declaration_resolves() -> None:
    spec = _spec()
    assert resolve_tool_read_only(spec, "reader") is True
    assert resolve_tool_read_only(spec, "writer") is False
    assert resolve_tool_read_only(spec, "plain") is None
    assert resolve_tool_read_only(spec, "unknown_tool") is None
    assert resolve_tool_read_only(None, "reader") is None


@pytest.mark.parametrize(
    ("trust", "ro_tools", "hint", "expected"),
    [
        # Explicit YAML wins over everything.
        (False, ["get_issue"], None, True),
        (True, ["get_issue"], False, True),
        # Trusted hint honoured both ways.
        (True, None, True, True),
        (True, None, False, False),
        # Untrusted hint ignored.
        (False, None, True, None),
        # Nothing declared: undeclared.
        (True, None, None, None),
    ],
)
def test_mcp_precedence(
    trust: bool, ro_tools: list[str] | None, hint: bool | None, expected: bool | None
) -> None:
    hints = {} if hint is None else {"github__get_issue": hint}
    spec = _spec(trust=trust, ro_tools=ro_tools)
    assert resolve_tool_read_only(spec, "github__get_issue", mcp_hints=hints) is expected
    # Claude-native spells MCP tools ``mcp__<server>__<tool>``.
    assert resolve_tool_read_only(spec, "mcp__github__get_issue", mcp_hints=hints) is expected


def test_mcp_tool_of_other_server_not_matched() -> None:
    spec = _spec(ro_tools=["get_issue"])
    assert resolve_tool_read_only(spec, "gitlab__get_issue") is None
    # A trusted server's hint never applies to another server's tool.
    trusted = _spec(trust=True)
    assert resolve_tool_read_only(trusted, "gitlab__x", mcp_hints={"gitlab__x": True}) is None


# ── MCP hint extraction ─────────────────────────────────────────────────


def test_mcp_read_only_hint_extraction() -> None:
    assert mcp_read_only_hint(SimpleNamespace(annotations=None)) is None
    assert (
        mcp_read_only_hint(SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=None))) is None
    )
    assert (
        mcp_read_only_hint(SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=True))) is True
    )
    assert (
        mcp_read_only_hint(SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=False)))
        is False
    )


def test_mcp_read_only_hint_from_real_mcp_tool() -> None:
    from mcp.types import Tool, ToolAnnotations

    tool = Tool(name="get_issue", inputSchema={}, annotations=ToolAnnotations(readOnlyHint=True))
    assert mcp_read_only_hint(tool) is True


# ── helper ──────────────────────────────────────────────────────────────


def test_tool_call_context_carries_read_only() -> None:
    ctx = tool_call_context(_spec(), "reader", {"q": 1}, actor={"run_as": "a@b"})
    assert ctx.phase == Phase.TOOL_CALL
    assert ctx.tool_name == "reader"
    assert ctx.actor == {"run_as": "a@b"}
    assert ctx.content == {"name": "reader", "arguments": {"q": 1}, "read_only": True}


def test_tool_call_context_undeclared_is_null() -> None:
    ctx = tool_call_context(_spec(), "plain", {})
    assert "read_only" in ctx.content
    assert ctx.content["read_only"] is None


def test_tool_call_context_unknown_sub_agent_falls_back_to_parent_spec() -> None:
    ctx = tool_call_context(_spec(), "reader", {}, sub_agent_name="no_such_child")
    assert ctx.content["read_only"] is True


def test_tool_call_context_applies_trusted_mcp_hints() -> None:
    hints = {"github__a": True, "github__b": False}
    spec = _spec(trust=True)
    assert tool_call_context(spec, "github__a", {}, mcp_hints=hints).content["read_only"] is True
    assert tool_call_context(spec, "github__b", {}, mcp_hints=hints).content["read_only"] is False
    # Cache miss and untrusted spec both stay null; YAML still wins.
    assert tool_call_context(spec, "github__a", {}, mcp_hints=None).content["read_only"] is None
    untrusted = _spec(trust=False)
    assert (
        tool_call_context(untrusted, "github__a", {}, mcp_hints=hints).content["read_only"] is None
    )
    listed = _spec(trust=True, ro_tools=["b"])
    assert tool_call_context(listed, "github__b", {}, mcp_hints=hints).content["read_only"] is True


def test_every_tool_call_context_goes_through_helper() -> None:
    """No production code builds a TOOL_CALL EvaluationContext inline.

    An inline construction silently omits ``data.read_only`` on that path,
    so a policy that reads it would see a declared read-only tool as undeclared.
    """
    helper = _REPO / "omnigent" / "policies" / "tool_read_only.py"
    offenders = [
        f"{path.relative_to(_REPO)}:{node.lineno}"
        for path in (_REPO / "omnigent").rglob("*.py")
        if path != helper
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "EvaluationContext"
        and any(
            kw.arg == "phase" and ast.unparse(kw.value) == "Phase.TOOL_CALL"
            for kw in node.keywords
        )
    ]
    assert offenders == [], f"build TOOL_CALL contexts via tool_call_context(): {offenders}"
