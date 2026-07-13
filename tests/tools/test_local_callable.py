"""Unit tests for omnigent-style in-process callable tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.spec.types import LocalToolInfo, ToolRuntime
from omnigent.tools.base import ToolContext
from omnigent.tools.local_callable import LocalCallableTool, load_local_callable_tools

_TEST_CTX = ToolContext(task_id="task_test", agent_id="agent_test")
_CALLABLE_LANGUAGE = "omnigent-python-callable"


@pytest.fixture()
def callable_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an importable temp module with local-callable test targets."""
    package_dir = tmp_path / "local_callable_targets"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "tool_fns.py").write_text(
        '''
class NotJson:
    def __repr__(self) -> str:
        return "<NotJson value>"


def describe(name: str, punctuation: str = "!") -> str:
    """Describe a name.

    Extra detail that should not reach the tool schema.
    """
    return f"hello {name}{punctuation}"


def returns_none() -> None:
    return None


def returns_structured() -> dict[str, object]:
    return {"ok": True, "items": [1, "two"]}


def returns_unserializable() -> NotJson:
    return NotJson()


def undocumented() -> str:
    return "ok"


not_callable = "plain string"
'''.lstrip()
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "local_callable_targets.tool_fns"


def _info(
    name: str,
    path: str | None,
    *,
    language: str = _CALLABLE_LANGUAGE,
    parameters: dict[str, object] | None = None,
    runtime: ToolRuntime = ToolRuntime.SERVER,
) -> LocalToolInfo:
    return LocalToolInfo(
        name=name,
        path=path,
        language=language,
        parameters=parameters,
        runtime=runtime,
    )


def test_name_dotted_path_and_schema_use_info_and_docstring(
    callable_module: str,
) -> None:
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "punctuation": {"type": "string"},
        },
        "required": ["name"],
    }
    tool = LocalCallableTool(
        _info(
            "describe",
            f"{callable_module}.describe",
            parameters=parameters,
        )
    )

    assert tool.name() == "describe"
    assert tool.dotted_path() == f"{callable_module}.describe"
    assert tool.get_schema() == {
        "type": "function",
        "function": {
            "name": "describe",
            "description": "Describe a name.",
            "parameters": parameters,
        },
    }


def test_schema_defaults_parameters_and_description_for_undocumented_callable(
    callable_module: str,
) -> None:
    tool = LocalCallableTool(_info("undocumented", f"{callable_module}.undocumented"))

    schema = tool.get_schema()

    assert schema["function"]["description"] == "User function tool 'undocumented'."
    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_invoke_passes_json_object_arguments_as_kwargs(callable_module: str) -> None:
    tool = LocalCallableTool(_info("describe", f"{callable_module}.describe"))

    result = tool.invoke(
        json.dumps({"name": "Ada", "punctuation": "?"}),
        _TEST_CTX,
    )

    assert result == "hello Ada?"


def test_invoke_stringifies_supported_return_shapes(callable_module: str) -> None:
    assert (
        LocalCallableTool(_info("describe", f"{callable_module}.describe")).invoke(
            json.dumps({"name": "Ada"}),
            _TEST_CTX,
        )
        == "hello Ada!"
    )
    assert (
        LocalCallableTool(_info("returns_none", f"{callable_module}.returns_none")).invoke(
            "{}",
            _TEST_CTX,
        )
        == ""
    )
    assert json.loads(
        LocalCallableTool(
            _info("returns_structured", f"{callable_module}.returns_structured")
        ).invoke("{}", _TEST_CTX)
    ) == {"ok": True, "items": [1, "two"]}
    assert (
        LocalCallableTool(
            _info("returns_unserializable", f"{callable_module}.returns_unserializable")
        ).invoke("{}", _TEST_CTX)
        == "<NotJson value>"
    )


@pytest.mark.parametrize("arguments", ["{", "[1, 2]", '"text"', "42"])
def test_invoke_rejects_malformed_or_non_object_json(
    callable_module: str,
    arguments: str,
) -> None:
    tool = LocalCallableTool(_info("describe", f"{callable_module}.describe"))

    with pytest.raises(ValueError):
        tool.invoke(arguments, _TEST_CTX)


@pytest.mark.parametrize(
    ("path", "exc_type", "match"),
    [
        ("not_dotted", ImportError, "invalid path"),
        ("missing_package.tool_fn", ModuleNotFoundError, "missing_package"),
        ("local_callable_targets.tool_fns.missing", AttributeError, "no attribute"),
        ("local_callable_targets.tool_fns.not_callable", TypeError, "non-callable"),
    ],
)
def test_resolution_failures_are_explicit(
    callable_module: str,
    path: str,
    exc_type: type[Exception],
    match: str,
) -> None:
    del callable_module
    tool = LocalCallableTool(_info("broken", path))

    with pytest.raises(exc_type, match=match):
        tool.get_schema()


def test_load_local_callable_tools_filters_to_omnigent_server_callables() -> None:
    loaded = load_local_callable_tools(
        [
            _info("callable_tool", "pkg.mod.callable_tool"),
            _info("native_python", "tools/python/native_python.py", language="python"),
            _info(
                "uc_function",
                None,
                runtime=ToolRuntime.UC_FUNCTION,
            ),
            _info("unimportable", "does.not.exist"),
        ]
    )

    assert [tool.name() for tool in loaded] == ["callable_tool", "unimportable"]
    assert [tool.dotted_path() for tool in loaded] == [
        "pkg.mod.callable_tool",
        "does.not.exist",
    ]
