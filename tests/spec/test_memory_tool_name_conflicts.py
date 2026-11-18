"""An agent's own tool named like a memory tool keeps the name; the agent goes without memory.

The memory tool names aren't reserved. The runner routes them to the memory
server only for an agent with memory on, so an agent that already has a tool by
one of these names keeps it, exactly as before memory existed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec import load
from omnigent.spec.parser import parse
from omnigent.spec.types import MEMORY_TOOL_NAMES
from omnigent.spec.validator import validate

_NAMES = sorted(MEMORY_TOOL_NAMES)
_CALLABLE = "tests.resources.examples._shared.tool_functions.get_current_time"


def _agent(tmp_path: Path, *, tool_groups: dict[str, bool] | None = None) -> Path:
    config: dict[str, object] = {
        "spec_version": 1,
        "name": "own-memory",
        "executor": {"config": {"harness": "claude-sdk"}},
    }
    if tool_groups is not None:
        config["tool_groups"] = tool_groups
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


def _with_local_tool(root: Path, name: str) -> Path:
    (root / "tools" / "python").mkdir(parents=True)
    (root / "tools" / "python" / f"{name}.py").write_text("def handler(args): return 'own'\n")
    return root


def _with_mcp_server(root: Path, name: str) -> Path:
    (root / "tools" / "mcp").mkdir(parents=True)
    (root / "tools" / "mcp" / "server.yaml").write_text(
        f"name: {name}\ntransport: http\nurl: http://localhost:9000/mcp\n"
    )
    return root


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("add", [_with_local_tool, _with_mcp_server], ids=["local", "mcp"])
def test_an_agents_own_tool_keeps_the_name_and_the_agent_loses_memory(
    tmp_path: Path, name: str, add, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        spec = parse(add(_agent(tmp_path), name))

    assert spec.tool_groups.memory is False
    assert name in {t.name for t in spec.local_tools} | {m.name for m in spec.mcp_servers}
    assert validate(spec).errors == []
    assert not caplog.records


@pytest.mark.parametrize(
    "exports", [["memory_get"], ["lookup", "memory_list"]], ids=["one", "several"]
)
def test_a_python_tool_counts_by_the_names_its_file_defines(
    tmp_path: Path, exports: list[str]
) -> None:
    # The loader registers each @tool function under its own name, whatever the file is called.
    root = _agent(tmp_path)
    (root / "tools" / "python").mkdir(parents=True)
    (root / "tools" / "python" / "helpers.py").write_text(
        "from omnigent_client.tools import tool\n"
        + "".join(
            f"\n\n@tool\ndef {name}(query: str) -> str:\n    return 'own'\n" for name in exports
        )
    )

    spec = parse(root)

    assert spec.tool_groups.memory is False
    assert validate(spec).errors == []


_OWN_MEMORY_GET = '@tool\ndef memory_get(path: str) -> str:\n    """Own."""\n    return \'own\'\n'


def _indented(source: str) -> str:
    return "".join(f"    {line}\n" if line else "\n" for line in source.splitlines())


@pytest.mark.parametrize(
    "source",
    [
        "if True:\n" + _indented(_OWN_MEMORY_GET),
        "try:\n    import json\nexcept ImportError:\n    pass\nelse:\n"
        + _indented(_OWN_MEMORY_GET),
        "import contextlib\n\nwith contextlib.suppress(ImportError):\n"
        + _indented(_OWN_MEMORY_GET),
    ],
    ids=["if-block", "try-else", "with-block"],
)
def test_a_tool_defined_inside_a_module_level_block_counts(tmp_path: Path, source: str) -> None:
    from omnigent.tools.manager import ToolManager

    root = _agent(tmp_path)
    (root / "tools" / "python").mkdir(parents=True)
    (root / "tools" / "python" / "helpers.py").write_text(
        "from omnigent_client.tools import tool\n\n" + source
    )

    spec = parse(root)

    assert spec.tool_groups.memory is False
    # The real loader registers the agent's own memory_get, and no memory tool replaces it.
    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in ToolManager(spec, workdir=root).get_tool_schemas()
    }
    assert schemas["memory_get"]["description"] == "Own."
    assert not (set(_NAMES) - {"memory_get"}) & set(schemas)


def test_unrelated_names_leave_memory_on(tmp_path: Path) -> None:
    root = _with_local_tool(_agent(tmp_path), "memory_helper")

    assert parse(root).tool_groups.memory is True


@pytest.mark.parametrize("add", [_with_local_tool, _with_mcp_server], ids=["local", "mcp"])
def test_turning_memory_on_explicitly_next_to_a_clash_is_rejected(tmp_path: Path, add) -> None:
    root = add(_agent(tmp_path, tool_groups={"memory": True}), "memory_get")

    with pytest.raises(OmnigentError, match="memory_get") as excinfo:
        parse(root)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


def test_turning_memory_off_explicitly_next_to_a_clash_is_fine(tmp_path: Path) -> None:
    root = _with_local_tool(_agent(tmp_path, tool_groups={"memory": False}), "memory_get")

    assert parse(root).tool_groups.memory is False


def _single_file(tmp_path: Path, tools: dict[str, object]) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.dump(
            {
                "name": "own-memory",
                "prompt": "You are a test agent.",
                "executor": {"model": "openai/gpt-5", "harness": "claude-sdk"},
                "tools": tools,
            }
        )
    )
    return path


def test_the_single_file_dialect_applies_the_same_rule(tmp_path: Path) -> None:
    tools = {"memory_write": {"type": "function", "description": "Own.", "callable": _CALLABLE}}

    spec = load(_single_file(tmp_path, tools))

    assert spec.tool_groups.memory is False
    assert [t.name for t in spec.local_tools] == ["memory_write"]


def test_an_inline_sub_agent_with_a_clashing_tool_loses_memory_alone(tmp_path: Path) -> None:
    tools = {
        "helper": {
            "type": "agent",
            "prompt": "Help.",
            "tools": {
                "memory_get": {"type": "function", "description": "Own.", "callable": _CALLABLE}
            },
        }
    }

    spec = load(_single_file(tmp_path, tools))

    assert spec.tool_groups.memory is True
    assert spec.sub_agents[0].tool_groups.memory is False
