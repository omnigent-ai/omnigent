"""Tests for the local Python tool subprocess entrypoint."""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from omnigent_client.tools import ToolState, tool
from pydantic import BaseModel

from omnigent.tools import _runner


@tool
def _string_tool() -> str:
    """Return a plain string."""
    return "ok"


class _ToolResult(BaseModel):
    value: int


@tool
def _structured_tool() -> _ToolResult:
    """Return a typed result."""
    return _ToolResult(value=7)


@tool
def _stateful_tool(value: str, tool_state: ToolState) -> str:
    """Record a value in ToolState."""
    return value


class _BadStr:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


@tool
def _bad_repr_tool() -> _BadStr:
    """Return a value that cannot be stringified by json fallback."""
    return _BadStr()


class _DummyStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def fileno(self) -> int:
        return 1


def _write_tool_module(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return path


def _run_main(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | bytes) -> dict[str, Any]:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(_runner.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(_runner, "_write_response", captured.append)

    _runner.main()

    assert captured, "main() did not emit a response"
    return captured[0]


def test_load_module_rejects_empty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(_runner, "_write_error", seen.append)

    assert _runner._load_module("") is None
    assert seen == ["Empty module_path in request"]


def test_load_module_imports_python_file(tmp_path: Path) -> None:
    module_path = _write_tool_module(
        tmp_path,
        "simple_tool.py",
        """
        value = 41
        """,
    )

    module = _runner._load_module(str(module_path))

    assert module is not None
    assert module.value == 41


def test_load_module_reports_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_tool_module(
        tmp_path,
        "broken_tool.py",
        """
        raise RuntimeError("boom")
        """,
    )
    seen: list[str] = []
    monkeypatch.setattr(_runner, "_write_error", seen.append)

    assert _runner._load_module(str(module_path)) is None
    assert seen and seen[0].startswith("Import error: RuntimeError: boom")


@pytest.mark.parametrize(
    ("tool_name", "value", "expected"),
    [
        ("missing", None, "Tool function 'missing' not found in module."),
        ("plain_value", "not callable", "Object 'plain_value' in module is not callable."),
        ("undecorated", lambda: None, "Function 'undecorated' is not decorated with @tool."),
    ],
)
def test_resolve_tool_function_rejects_invalid_targets(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    value: object,
    expected: str,
) -> None:
    module = ModuleType("test_module")
    if tool_name != "missing":
        setattr(module, tool_name, value)
    seen: list[str] = []
    monkeypatch.setattr(_runner, "_write_error", seen.append)

    assert _runner._resolve_tool_function(module, tool_name) is None
    assert seen == [expected]


def test_resolve_tool_function_accepts_decorated_tool() -> None:
    module = ModuleType("test_module")
    module.string_tool = _string_tool

    assert _runner._resolve_tool_function(module, "string_tool") is _string_tool


def test_maybe_inject_tool_state_inserts_live_tool_state(tmp_path: Path) -> None:
    arguments: dict[str, Any] = {"value": "x"}

    _runner._maybe_inject_tool_state(_stateful_tool, arguments, str(tmp_path / "tool-state"))

    assert arguments["value"] == "x"
    assert isinstance(arguments["tool_state"], ToolState)
    arguments["tool_state"].set("seen", True)
    assert (tmp_path / "tool-state").exists()


def test_maybe_inject_tool_state_ignores_tools_without_reserved_parameter() -> None:
    arguments = {"value": "x"}

    _runner._maybe_inject_tool_state(_string_tool, arguments, "/tmp/unused")

    assert arguments == {"value": "x"}


def test_construct_tool_state_requires_state_root() -> None:
    with pytest.raises(RuntimeError, match="no state_root was provided"):
        _runner._construct_tool_state(None)


def test_serialize_result_passes_strings_through() -> None:
    assert _runner._serialize_result(_string_tool, "hello") == "hello"


def test_serialize_result_uses_return_annotation_type_adapter() -> None:
    result = _structured_tool()

    assert json.loads(_runner._serialize_result(_structured_tool, result)) == {"value": 7}


def test_serialize_result_falls_back_to_json_for_unannotated_target() -> None:
    def undecorated() -> None:
        return None

    assert _runner._serialize_result(undecorated, {"ok": True}) == '{"ok": true}'


def test_serialize_result_reports_unserializable_values() -> None:
    loop = []
    loop.append(loop)

    rendered = _runner._serialize_result(_bad_repr_tool, loop)

    assert rendered == "<unserializable return value: list: Circular reference detected>"


def test_get_output_fd_uses_stdout_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_AP_RESPONSE_MODE", "stdout")
    monkeypatch.setattr(_runner.sys, "stdout", _DummyStdout())

    assert _runner._get_output_fd() == 1


def test_get_output_fd_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_AP_RESPONSE_MODE", raising=False)
    monkeypatch.setenv("_AP_RESPONSE_FD", "9")

    assert _runner._get_output_fd() == 9


def test_write_response_prefixes_stdout_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _DummyStdout()
    monkeypatch.setenv("_AP_RESPONSE_MODE", "stdout")
    monkeypatch.setattr(_runner.sys, "stdout", stdout)

    _runner._write_response({"result": "ok"})

    assert stdout.buffer.getvalue() == b'__AP_RESPONSE__:{"result": "ok"}\n'


def test_main_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_main(monkeypatch, b"{")

    assert result["error"].startswith("Invalid request JSON:")


def test_main_requires_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_main(monkeypatch, {"module_path": "/tmp/tool.py", "arguments": {}})

    assert result["error"] == "Request missing 'tool_name' field — runner cannot dispatch."


def test_main_executes_tool_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_tool_module(
        tmp_path,
        "tool_mod.py",
        """
        from omnigent_client.tools import tool

        @tool
        def greet(name: str) -> str:
            return f"hi {name}"
        """,
    )

    result = _run_main(
        monkeypatch,
        {
            "module_path": str(module_path),
            "tool_name": "greet",
            "arguments": {"name": "Ada"},
        },
    )

    assert result == {"result": "hi Ada"}


def test_main_injects_tool_state_for_stateful_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "tool-state"
    module_path = _write_tool_module(
        tmp_path,
        "stateful_tool.py",
        """
        from omnigent_client.tools import ToolState, tool

        @tool
        def remember(value: str, tool_state: ToolState) -> str:
            previous = tool_state.get("value")
            tool_state.set("value", value)
            return f"{previous}|{value}"
        """,
    )

    first = _run_main(
        monkeypatch,
        {
            "module_path": str(module_path),
            "tool_name": "remember",
            "arguments": {"value": "one"},
            "state_root": str(state_root),
        },
    )
    second = _run_main(
        monkeypatch,
        {
            "module_path": str(module_path),
            "tool_name": "remember",
            "arguments": {"value": "two"},
            "state_root": str(state_root),
        },
    )

    assert first == {"result": "None|one"}
    assert second == {"result": "one|two"}


def test_main_surfaces_tool_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_tool_module(
        tmp_path,
        "boom_tool.py",
        """
        from omnigent_client.tools import tool

        @tool
        def explode() -> str:
            raise RuntimeError("boom")
        """,
    )

    result = _run_main(
        monkeypatch,
        {
            "module_path": str(module_path),
            "tool_name": "explode",
            "arguments": {},
        },
    )

    assert result == {"error": "RuntimeError: boom"}
