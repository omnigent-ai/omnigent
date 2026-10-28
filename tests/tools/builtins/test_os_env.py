"""``sys_os_*`` tools: UTF-8-safe results and ``sys_os_shell`` argument-name validation."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import pytest

from omnigent.inner.os_env import OSEnvironment
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.os_env import SysOsReadTool, SysOsShellTool


class _FakeOSEnvironment:
    def __init__(
        self,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result or {}
        self._error = error

    async def read(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        if self._error is not None:
            raise self._error
        return self._result


class _RecordingOSEnvironment:
    """Records ``shell`` calls so a test can assert what reached the environment."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def shell(self, command: str, timeout: int | None = None) -> dict[str, Any]:
        self.calls.append({"command": command, "timeout": timeout})
        return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}


def test_invoke_keeps_unicode_readable_and_surrogates_transport_safe(
    tool_ctx: ToolContext,
) -> None:
    result = {"content": "Привет 世界", "path": "recording-\udcff.txt"}
    tool = SysOsReadTool(cast(OSEnvironment, _FakeOSEnvironment(result=result)))

    serialized = tool.invoke(json.dumps({"path": "recording.txt"}), tool_ctx)

    assert "Привет 世界" in serialized
    assert "\\udcff" in serialized
    serialized.encode("utf-8")
    assert json.loads(serialized) == result


def test_invoke_error_keeps_unicode_readable_and_surrogates_transport_safe(
    caplog: pytest.LogCaptureFixture,
    tool_ctx: ToolContext,
) -> None:
    error = RuntimeError("ошибка для recording-\udcff.txt")
    tool = SysOsReadTool(cast(OSEnvironment, _FakeOSEnvironment(error=error)))

    caplog.set_level(logging.CRITICAL + 1, logger="omnigent.tools.builtins.os_env")
    serialized = tool.invoke(json.dumps({"path": "recording.txt"}), tool_ctx)

    assert "ошибка" in serialized
    assert "\\udcff" in serialized
    serialized.encode("utf-8")
    assert json.loads(serialized) == {"error": str(error)}


def _invoke_shell(
    tool: SysOsShellTool, arguments: dict[str, Any], tool_ctx: ToolContext
) -> dict[str, Any]:
    return json.loads(tool.invoke(json.dumps(arguments), tool_ctx))


def test_sys_os_shell_rejects_unknown_argument_names(tool_ctx: ToolContext) -> None:
    os_env = _RecordingOSEnvironment()
    tool = SysOsShellTool(cast(OSEnvironment, os_env))

    result = _invoke_shell(tool, {"command": "sleep 300", "timeout_seconds": 500}, tool_ctx)

    assert "error" in result, result
    assert "timeout_seconds" in result["error"]
    assert "command, timeout" in result["error"]
    assert os_env.calls == []


def test_sys_os_shell_forwards_declared_arguments(tool_ctx: ToolContext) -> None:
    os_env = _RecordingOSEnvironment()
    tool = SysOsShellTool(cast(OSEnvironment, os_env))

    result = _invoke_shell(tool, {"command": "sleep 1", "timeout": 500}, tool_ctx)

    assert result["exit_code"] == 0
    assert os_env.calls == [{"command": "sleep 1", "timeout": 500}]


def test_sys_os_shell_schema_declares_no_additional_properties() -> None:
    parameters = SysOsShellTool.get_schema()["function"]["parameters"]

    assert parameters.get("additionalProperties") is False, parameters
    assert sorted(parameters["properties"]) == ["command", "timeout"]
