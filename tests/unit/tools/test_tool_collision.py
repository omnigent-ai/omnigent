"""Tests for G27 — tool name collisions fail loud at agent image load.

The collision-detection logic itself lives in
``omnigent.tools.local.load_local_python_tools``; these tests
exercise it through the loader's public interface with realistic
agent-image directory layouts.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from omnigent.spec.types import LocalToolInfo
from omnigent.tools.local import LocalToolLoadError, load_local_python_tools


def _write_tool(py_dir: Path, filename: str, func_name: str) -> None:
    """Write a one-tool file with the given decorated function name."""
    py_dir.mkdir(parents=True, exist_ok=True)
    code = (
        '"""tool fixture."""\n'
        "from omnigent_client import tool\n"
        "\n"
        "\n"
        "@tool\n"
        f"def {func_name}(x: str) -> str:\n"
        '    """Doc."""\n'
        f'    return f"{func_name}: " + x\n'
    )
    (py_dir / filename).write_text(code)


def test_one_custom_tool_no_collision_loads_fine(tmp_path: Path) -> None:
    """Sanity: a single non-colliding custom tool loads cleanly."""
    py_dir = tmp_path / "tools" / "python"
    _write_tool(py_dir, "alpha.py", "alpha")
    info = LocalToolInfo(name="alpha", path="tools/python/alpha.py", language="python")
    tools = load_local_python_tools(
        [info],
        tmp_path,
        agent_name="testagent",
        builtin_tool_names=frozenset({"web_search", "code_sandbox"}),
    )
    assert len(tools) == 1
    assert tools[0].name() == "alpha"


def test_two_custom_tools_same_name_fails_load(tmp_path: Path) -> None:
    """Two custom tool files defining the same @tool name fail loud."""
    py_dir = tmp_path / "tools" / "python"
    _write_tool(py_dir, "first.py", "duplicate_name")
    _write_tool(py_dir, "second.py", "duplicate_name")
    infos = [
        LocalToolInfo(name="first", path="tools/python/first.py", language="python"),
        LocalToolInfo(name="second", path="tools/python/second.py", language="python"),
    ]
    with pytest.raises(LocalToolLoadError, match="collision") as exc_info:
        load_local_python_tools(infos, tmp_path, agent_name="testagent")
    msg = str(exc_info.value)
    # Both source paths AND the colliding name must appear so the
    # author can directly navigate to one of them and rename.
    assert "first.py" in msg
    assert "second.py" in msg
    assert "duplicate_name" in msg


def test_custom_tool_name_collides_with_builtin_fails_load(
    tmp_path: Path,
) -> None:
    """Custom tool whose name matches a built-in tool fails to load."""
    py_dir = tmp_path / "tools" / "python"
    _write_tool(py_dir, "my_search.py", "web_search")
    info = LocalToolInfo(name="my_search", path="tools/python/my_search.py", language="python")
    with pytest.raises(LocalToolLoadError, match="collision") as exc_info:
        load_local_python_tools(
            [info],
            tmp_path,
            agent_name="testagent",
            builtin_tool_names=frozenset({"web_search"}),
        )
    msg = str(exc_info.value)
    # The error must name the custom file path, the builtin, AND
    # the agent so the operator gets full context for fixing it.
    assert "my_search.py" in msg
    assert "web_search" in msg
    assert "testagent" in msg


def test_collision_error_message_is_actionable(tmp_path: Path) -> None:
    """Collision errors include a remediation hint."""
    py_dir = tmp_path / "tools" / "python"
    _write_tool(py_dir, "ws.py", "web_search")
    info = LocalToolInfo(name="ws", path="tools/python/ws.py", language="python")
    with pytest.raises(LocalToolLoadError) as exc_info:
        load_local_python_tools(
            [info],
            tmp_path,
            agent_name="testagent",
            builtin_tool_names=frozenset({"web_search"}),
        )
    msg = str(exc_info.value)
    # Remediation hint: tells the author the two ways to resolve.
    assert "rename" in msg.lower() or "remove" in msg.lower()


def test_no_collision_with_unrelated_builtin_loads_fine(
    tmp_path: Path,
) -> None:
    """Custom tools whose names differ from all builtins load fine."""
    py_dir = tmp_path / "tools" / "python"
    _write_tool(py_dir, "unique.py", "my_unique_name")
    info = LocalToolInfo(name="unique", path="tools/python/unique.py", language="python")
    tools = load_local_python_tools(
        [info],
        tmp_path,
        agent_name="testagent",
        builtin_tool_names=frozenset({"web_search", "code_sandbox", "web_fetch"}),
    )
    assert len(tools) == 1
    assert tools[0].name() == "my_unique_name"


def test_multi_tool_file_collision_with_builtin(tmp_path: Path) -> None:
    """Among multiple @tool functions in one file, any collision still fails."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    multi = textwrap.dedent(
        '''\
        """Multi-tool file with one colliding name."""
        from omnigent_client import tool


        @tool
        def safe_one(x: str) -> str:
            """OK."""
            return x


        @tool
        def web_search(q: str) -> str:
            """Collides with the builtin."""
            return q
        '''
    )
    (py_dir / "multi.py").write_text(multi)
    info = LocalToolInfo(name="multi", path="tools/python/multi.py", language="python")
    with pytest.raises(LocalToolLoadError, match="collision") as exc_info:
        load_local_python_tools(
            [info],
            tmp_path,
            agent_name="testagent",
            builtin_tool_names=frozenset({"web_search"}),
        )
    assert "web_search" in str(exc_info.value)
