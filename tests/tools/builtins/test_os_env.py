"""Tests for UTF-8-safe ``sys_os_*`` tool result serialization."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from omnigent.inner.os_env import OSEnvironment
from omnigent.inner.os_env_pdf import read_pdf_pages
from omnigent.runtime.mcp_tool_result import decode_mcp_image_result
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.os_env import SysOsReadTool
from tests.inner.test_os_env_pdf import make_pdf


class _FakeOSEnvironment:
    def __init__(
        self,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result or {}
        self._error = error
        self.read_kwargs: dict[str, object] = {}

    async def read(self, **kwargs: object) -> dict[str, object]:
        self.read_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


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


def test_sys_os_read_pdf_returns_image_envelope(tmp_path: Path, tool_ctx: ToolContext) -> None:
    environment = _FakeOSEnvironment(result=read_pdf_pages(make_pdf(tmp_path, ["Alpha"]), None))
    tool = SysOsReadTool(cast(OSEnvironment, environment))

    decoded = decode_mcp_image_result(
        json.loads(tool.invoke(json.dumps({"path": "doc.pdf", "pages": "1"}), tool_ctx))
    )

    assert environment.read_kwargs["pages"] == "1"
    assert environment.read_kwargs["pdf"] is True
    assert decoded is not None
    assert not decoded.is_error
    assert [block["type"] for block in decoded.content] == ["text", "text", "image"]
    assert json.loads(str(decoded.content[0]["text"]))["total_pages"] == 1
    assert str(decoded.content[1]["text"]).startswith("--- page 1 of 1 ---\nAlpha")
    assert decoded.content[2]["mimeType"] == "image/png"


def test_sys_os_read_pdf_blank_page_marked(tmp_path: Path, tool_ctx: ToolContext) -> None:
    environment = _FakeOSEnvironment(result=read_pdf_pages(make_pdf(tmp_path, [""]), None))
    tool = SysOsReadTool(cast(OSEnvironment, environment))

    decoded = decode_mcp_image_result(
        json.loads(tool.invoke(json.dumps({"path": "doc.pdf"}), tool_ctx))
    )

    assert decoded is not None
    assert "(no extractable text)" in str(decoded.content[1]["text"])


def test_sys_os_read_schema_documents_pages() -> None:
    assert "pages" in SysOsReadTool.get_schema()["function"]["parameters"]["properties"]


def test_sys_os_read_integer_pages_forwarded_as_text(tool_ctx: ToolContext) -> None:
    environment = _FakeOSEnvironment(result={"content": ""})
    tool = SysOsReadTool(cast(OSEnvironment, environment))

    tool.invoke(json.dumps({"path": "doc.pdf", "pages": 2}), tool_ctx)

    assert environment.read_kwargs["pages"] == "2"
