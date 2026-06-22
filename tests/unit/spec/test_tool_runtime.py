"""Tests for the ``runtime: server | client`` tool-spec field.

Covers the end-to-end contract — YAML parser (via the omnigent
inner-stack loader, which is the entry point every YAML spec
flows through today), the spec translator, and the omnigent
validator. The runtime path that emits ``action_required`` for
client-runtime tools lives in a separate stack and is intentionally
not exercised here.
"""

from __future__ import annotations

import pytest

from omnigent.inner.loader import load_agent_def
from omnigent.inner.tools import FunctionTool
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    LocalToolInfo,
    ToolRuntime,
)
from omnigent.spec.validator import validate


def _load_tool(name: str, tool_data: object) -> FunctionTool:
    """
    Drive the public YAML loader to parse one tool entry and return
    the resulting :class:`FunctionTool`.

    Wraps :func:`load_agent_def` with a minimal valid agent envelope
    so tests can target the tool-parsing surface without each test
    re-stating the boilerplate. Using the public entry point (rather
    than the loader's ``_parse_tool`` private helper) means tests
    catch regressions in the surrounding plumbing too.

    :param name: YAML key the tool is declared under, e.g.
        ``"open_in_editor"``.
    :param tool_data: The raw mapping the YAML author would write
        under that key, e.g.
        ``{"type": "function", "runtime": "client", ...}``.
    :returns: The parsed :class:`FunctionTool`.
    :raises AssertionError: If the loader produced something other
        than a :class:`FunctionTool` (e.g. dispatched to a different
        ``type:`` branch).
    """
    agent_def = load_agent_def(
        {
            "name": "test_agent",
            "executor": {"model": "databricks-claude-sonnet-4"},
            "tools": {name: tool_data},
        },
    )
    tool = agent_def.tools[name]
    assert isinstance(tool, FunctionTool), tool
    return tool


def _minimal_spec(**overrides: object) -> AgentSpec:
    """Build a minimal valid AgentSpec with optional overrides."""
    defaults: dict[str, object] = {
        "spec_version": 1,
        "executor": ExecutorSpec(config={"harness": "claude-sdk"}),
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------
# Parser (omnigent inner-stack loader) coverage.
# ---------------------------------------------------------------


def test_parse_tool_explicit_runtime_server() -> None:
    tool = _load_tool(
        "search",
        {
            "type": "function",
            "runtime": "server",
            "description": "Search.",
            "callable": "tests.resources.examples._shared.tool_functions.web_search",
        },
    )
    assert tool.runtime == "server"
    assert tool.callable is not None


def test_parse_tool_runtime_default_is_server() -> None:
    tool = _load_tool(
        "search",
        {
            "type": "function",
            "description": "Search.",
            "callable": "tests.resources.examples._shared.tool_functions.web_search",
        },
    )
    assert tool.runtime == "server"


def test_parse_tool_runtime_client_no_callable() -> None:
    tool = _load_tool(
        "open_in_editor",
        {
            "type": "function",
            "runtime": "client",
            "description": "Open a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    )
    assert tool.runtime == "client"
    assert tool.callable is None


def test_parse_tool_runtime_client_with_callable_rejected() -> None:
    with pytest.raises(ValueError, match=r"must not.*callable"):
        _load_tool(
            "open_in_editor",
            {
                "type": "function",
                "runtime": "client",
                "callable": "tests.resources.examples._shared.tool_functions.web_search",
            },
        )


def test_parse_tool_unknown_runtime_rejected() -> None:
    with pytest.raises(ValueError, match="invalid runtime"):
        _load_tool(
            "search",
            {
                "type": "function",
                "runtime": "bogus",
                "callable": "tests.resources.examples._shared.tool_functions.web_search",
            },
        )


# ---------------------------------------------------------------
# Validator coverage on the parsed AgentSpec.
# ---------------------------------------------------------------


def test_validator_server_tool_with_path_valid() -> None:
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="search",
                path="tests.resources.examples._shared.tool_functions.web_search",
                language="python",
                runtime=ToolRuntime.SERVER,
            )
        ],
    )
    result = validate(spec)
    assert result.valid, result.errors


def test_validator_server_tool_without_path_rejected() -> None:
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="search",
                path=None,
                language="python",
                runtime=ToolRuntime.SERVER,
            )
        ],
    )
    result = validate(spec)
    assert not result.valid
    # The error must point at the offending entry's ``path`` and
    # explain the contract — assert on both so a future refactor
    # can't silently flip the field name.
    matching = [e for e in result.errors if e.path == "local_tools[0].path"]
    assert matching, result.errors
    assert "no callable path" in matching[0].message


def test_validator_client_tool_without_callable_valid() -> None:
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="open_in_editor",
                path=None,
                language="python",
                runtime=ToolRuntime.CLIENT,
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ],
    )
    result = validate(spec)
    assert result.valid, result.errors


def test_validator_client_tool_with_path_rejected() -> None:
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="open_in_editor",
                path="tests.resources.examples._shared.tool_functions.web_search",
                language="python",
                runtime=ToolRuntime.CLIENT,
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ],
    )
    result = validate(spec)
    assert not result.valid
    matching = [e for e in result.errors if e.path == "local_tools[0].path"]
    assert matching, result.errors
    assert "must NOT declare" in matching[0].message


def test_validator_client_tool_without_parameters_rejected() -> None:
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="open_in_editor",
                path=None,
                language="python",
                runtime=ToolRuntime.CLIENT,
                parameters=None,
            )
        ],
    )
    result = validate(spec)
    assert not result.valid
    matching = [e for e in result.errors if e.path == "local_tools[0].parameters"]
    assert matching, result.errors


def test_validator_default_runtime_is_server() -> None:
    # No ``runtime=`` argument — the dataclass default applies.
    info = LocalToolInfo(
        name="search",
        path="tests.resources.examples._shared.tool_functions.web_search",
        language="python",
    )
    assert info.runtime == ToolRuntime.SERVER
    spec = _minimal_spec(local_tools=[info])
    result = validate(spec)
    assert result.valid, result.errors


# ---------------------------------------------------------------
# Round-trip via the example bundle: existing YAMLs without
# ``runtime:`` must continue parsing identically.
# ---------------------------------------------------------------


def test_existing_example_yaml_parses_with_default_server_runtime() -> None:
    """``examples/agent_with_tools.yaml`` declares no ``runtime:``.

    Every tool should land on the default :attr:`ToolRuntime.SERVER`
    with the historical callable wired up — this proves the new
    field is purely additive for old specs.
    """
    agent_def = load_agent_def("tests/resources/examples/agent_with_tools.yaml")
    assert agent_def.tools, "fixture YAML must declare at least one tool"
    for tool_name, tool in agent_def.tools.items():
        assert isinstance(tool, FunctionTool), tool_name
        # Every tool defaults to server-runtime; the callable
        # resolved to a real Python object — this catches a
        # silent regression where the loader stops resolving
        # callables on default-runtime entries.
        assert tool.runtime == "server", tool_name
        assert tool.callable is not None, tool_name


def test_new_client_tool_example_yaml_parses() -> None:
    """``examples/agent_with_client_tools.yaml`` exercises both knobs.

    The example is the canonical reference for authors; if it
    stops parsing or its tools end up on the wrong runtime, every
    user copying from it inherits the breakage. Pin both directions.
    """
    agent_def = load_agent_def("tests/resources/examples/agent_with_client_tools.yaml")
    server_tool = agent_def.tools["search_web"]
    client_tool = agent_def.tools["open_in_editor"]
    assert isinstance(server_tool, FunctionTool)
    assert server_tool.runtime == "server"
    assert server_tool.callable is not None
    assert isinstance(client_tool, FunctionTool)
    assert client_tool.runtime == "client"
    assert client_tool.callable is None
