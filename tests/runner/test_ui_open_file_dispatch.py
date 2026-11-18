"""File-opening tool schema, validation and runner/native dispatch."""

import json

import httpx
import pytest
from pydantic import ValidationError

from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    execute_tool,
    should_dispatch_locally,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.ui import OpenFileRequest


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_open_file_is_available_to_native_and_local_dispatch(spec: AgentSpec | None) -> None:
    schema = next(
        tool for tool in build_native_relay_tool_schemas(spec) if tool["name"] == "ui_open_file"
    )
    assert schema["parameters"]["required"] == ["path"]
    assert schema["parameters"]["additionalProperties"] is False
    assert should_dispatch_locally("ui_open_file")


@pytest.mark.parametrize(
    "path",
    [
        "",
        " ",
        ".",
        "./",
        "/etc/passwd",
        "../x",
        "a/../../x",
        "~/x",
        "https://example.com/x",
        "C:\\x",
        "a\\b",
        "a\x00b",
        "a\nb",
        "folder/",
        "a" * 4097,
    ],
)
def test_open_file_rejects_invalid_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        OpenFileRequest(path=path)


@pytest.mark.parametrize("path", ["README.md", "docs/my notes.md", "目录/文件.md"])
def test_open_file_accepts_workspace_files(path: str) -> None:
    assert OpenFileRequest(path=path).path == path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (202, {"subscribers": 0}),
        (202, {"subscribers": 2}),
        (403, {"detail": "forbidden"}),
        (202, {}),
        (202, {"subscribers": True}),
    ],
)
async def test_dispatch_preserves_delivery_and_reports_failures(status: int, body: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/sessions/current/ui/open-file"
        assert json.loads(request.content) == {"path": "docs/guide.md"}
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = json.loads(
            await execute_tool(
                tool_name="ui_open_file",
                arguments='{"path":"./docs/guide.md"}',
                conversation_id="current",
                server_client=client,
            )
        )
    if status == 202 and type(body.get("subscribers")) is int:
        assert output == body
    else:
        assert "error" in output


@pytest.mark.asyncio
async def test_dispatch_rejects_cross_session_args_and_missing_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid arguments must not reach the server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name="ui_open_file",
            arguments='{"path":"README.md", "session_id":"other"}',
            conversation_id="current",
            server_client=client,
        )
        assert "error" in json.loads(output)
    output = await execute_tool(tool_name="ui_open_file", arguments='{"path":"README.md"}')
    assert "requires server access" in json.loads(output)["error"]
