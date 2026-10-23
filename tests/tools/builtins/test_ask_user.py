"""The question tool exposes data entry without caller-controlled identity."""

import pytest

from omnigent.spec.types import AgentSpec
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import BUILTIN_NAMES, INSTANTIABLE_BUILTINS, get_builtin_tool
from omnigent.tools.builtins.ask_user import SysAskUserTool


def test_ask_user_is_framework_owned_and_always_available() -> None:
    manager = ToolManager(AgentSpec(spec_version=1, async_enabled=False), os_env_schema_only=True)
    try:
        schema = next(
            s["function"]
            for s in manager.get_tool_schemas()
            if s["function"]["name"] == "sys_ask_user"
        )
    finally:
        manager.shutdown()
    assert "sys_ask_user" in BUILTIN_NAMES
    assert "sys_ask_user" not in INSTANTIABLE_BUILTINS
    assert get_builtin_tool("sys_ask_user") is None
    parameters = schema["parameters"]
    assert set(parameters["properties"]) == {"questions"}
    assert parameters["required"] == ["questions"]
    assert parameters["additionalProperties"] is False
    question = parameters["$defs"]["AsyncUserInputQuestion"]
    assert set(question["properties"]) == {"title", "options"}
    assert question["additionalProperties"] is False
    assert "Non-blocking" in schema["description"]
    assert "wait for the answers" in schema["description"]
    assert "does not request or grant authorization" in schema["description"]


def test_ask_user_cannot_be_auto_accepted_by_server_invocation(tool_ctx: ToolContext) -> None:
    with pytest.raises(NotImplementedError, match="runner-dispatched"):
        SysAskUserTool().invoke('{"questions": [{"title": "Proceed?"}]}', tool_ctx)
