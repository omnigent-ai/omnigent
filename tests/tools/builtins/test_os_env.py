"""Tests for UTF-8-safe ``sys_os_*`` tool result serialization."""

from __future__ import annotations

import json
from typing import cast

from omnigent.inner.os_env import OSEnvironment
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.os_env import SysOsReadTool


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


_CTX = ToolContext(task_id="task_test", agent_id="agent_test")


def test_invoke_keeps_unicode_readable_and_surrogates_transport_safe() -> None:
    result = {"content": "Привет 世界", "path": "recording-\udcff.txt"}
    tool = SysOsReadTool(cast(OSEnvironment, _FakeOSEnvironment(result=result)))

    serialized = tool.invoke(json.dumps({"path": "recording.txt"}), _CTX)

    assert "Привет 世界" in serialized
    assert "\\udcff" in serialized
    serialized.encode("utf-8")
    assert json.loads(serialized) == result


def test_invoke_error_keeps_unicode_readable_and_surrogates_transport_safe() -> None:
    error = RuntimeError("ошибка для recording-\udcff.txt")
    tool = SysOsReadTool(cast(OSEnvironment, _FakeOSEnvironment(error=error)))

    serialized = tool.invoke(json.dumps({"path": "recording.txt"}), _CTX)

    assert "ошибка" in serialized
    assert "\\udcff" in serialized
    serialized.encode("utf-8")
    assert json.loads(serialized) == {"error": str(error)}
