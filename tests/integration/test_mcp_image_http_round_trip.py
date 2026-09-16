"""Offline image-tool transport through the live server, runner, and stdio MCP.

These tests call the MCP HTTP endpoint directly; they do not start an agent
turn or contact the mock LLM. The companion stdio fixture serves the runner
with two distinct real images and essential text after both images.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import create_runner_bound_session, register_inline_agent
from tests.integration._stdio_image_tool import (
    BETWEEN_TEXT,
    EARLY_TEXT,
    LATE_TEXT,
    TINY_JPEG_BASE64,
    TINY_PNG_BASE64,
    TRANSFORMED_TEXT,
)

_TOOL_NAME = "image_mcp__snapshot"
_DENY_REASON = "image output blocked by test policy"


@pytest.fixture(scope="session")
def isolated_http_client(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[httpx.Client]:
    """Start the existing live stack with disposable config and no keyring."""
    config_home = tmp_path_factory.mktemp("mcp-image-config")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
        patch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
        # The live runner is a sibling Python process; pytest's pythonpath
        # setting does not propagate to it or its MCP subprocess.
        repo_root = Path(__file__).resolve().parents[2]
        sdk_root = repo_root / "sdks"
        patch.setenv(
            "PYTHONPATH",
            os.pathsep.join(
                (
                    str(repo_root),
                    str(sdk_root / "python-client"),
                    str(sdk_root / "ui"),
                    os.environ.get("PYTHONPATH", ""),
                )
            ),
        )
        base_url = request.getfixturevalue("live_server")
        with httpx.Client(base_url=base_url, timeout=45.0) as client:
            yield client


def _image_session(
    client: httpx.Client,
    *,
    runner_id: str,
    mock_url: str,
    deny_result: bool = False,
    transform_result: bool = False,
) -> str:
    """Register the real stdio fixture and bind a disposable session."""
    name = f"image-http-{uuid.uuid4().hex[:8]}"
    config: dict[str, Any] = {
        "tools": {
            "image_mcp": {
                "type": "mcp",
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("_stdio_image_tool.py"))],
            }
        }
    }
    if deny_result:
        config["policies"] = {
            "suppress_image_result": {
                "type": "function",
                "on": [f"tool_result:{_TOOL_NAME}"],
                "function": {
                    "path": "omnigent.policies.function.make_fixed_action_callable",
                    "arguments": {
                        "action": "deny",
                        "reason": _DENY_REASON,
                        "on_phases": ["tool_result"],
                        "on_tools": [_TOOL_NAME],
                    },
                },
            }
        }
    elif transform_result:
        config["policies"] = {
            "replace_image_result": {
                "type": "function",
                "on": [f"tool_result:{_TOOL_NAME}"],
                "function": {"path": "tests.integration._stdio_image_tool.replace_image_result"},
            }
        }
    registered = register_inline_agent(
        client,
        name=name,
        harness="openai-agents",
        model=f"mock-{name}",
        profile="",
        prompt="No agent turn is run by this transport test.",
        mock_llm_base_url=f"{mock_url}/v1",
        extra_config=config,
    )
    return create_runner_bound_session(client, agent_name=registered, runner_id=runner_id)


def _call_snapshot(
    client: httpx.Client, session_id: str, *, error: bool = False
) -> dict[str, Any]:
    response = client.post(
        f"/v1/sessions/{session_id}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 37,
            "method": "tools/call",
            "params": {"name": _TOOL_NAME, "arguments": {"error": error}},
        },
    )
    response.raise_for_status()
    payload = response.json()
    assert payload["id"] == 37
    assert "error" not in payload, payload
    return payload["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["codex", "native", "claude-sdk"])
@pytest.mark.parametrize("upstream_error", [False, True])
async def test_images_and_late_text_survive_production_http_and_adapters(
    isolated_http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    upstream_error: bool,
    adapter: str,
) -> None:
    """Distinct stdio images reach each adapter with required trailing text."""
    from omnigent.inner.codex_executor import _dynamic_tool_result_payload

    session_id = _image_session(
        isolated_http_client, runner_id=live_runner_id, mock_url=mock_llm_server_url
    )
    result = _call_snapshot(isolated_http_client, session_id, error=upstream_error)
    assert len(result["content"]) == 1
    wire_output = result["content"][0]["text"]
    envelope = json.loads(wire_output)
    assert envelope["isError"] is upstream_error
    assert [block["type"] for block in envelope["content"]] == [
        "text",
        "image",
        "text",
        "image",
        "text",
    ]
    assert envelope["content"][0]["text"] == EARLY_TEXT
    assert envelope["content"][1]["mimeType"] == "image/png"
    assert envelope["content"][1]["data"] == TINY_PNG_BASE64
    assert base64.b64decode(envelope["content"][1]["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert envelope["content"][2]["text"] == BETWEEN_TEXT
    assert envelope["content"][3]["mimeType"] == "image/jpeg"
    assert envelope["content"][3]["data"] == TINY_JPEG_BASE64
    assert base64.b64decode(envelope["content"][3]["data"]).startswith(b"\xff\xd8\xff")
    assert envelope["content"][4]["text"] == LATE_TEXT

    if adapter != "codex":
        if adapter == "native":
            from omnigent.harnesses.claude_native.bridge import _mcp_response_from_tool_result

            native = _mcp_response_from_tool_result(envelope)
        else:
            from claude_agent_sdk import create_sdk_mcp_server
            from mcp.types import CallToolRequest, CallToolRequestParams

            from omnigent.inner.claude_sdk_executor import _build_mcp_tools

            async def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                assert name == _TOOL_NAME
                return envelope

            tools = _build_mcp_tools([{"name": _TOOL_NAME, "description": "snapshot"}], execute)
            sdk_server = create_sdk_mcp_server(name="image-http", tools=tools)["instance"]
            response = await sdk_server.request_handlers[CallToolRequest](
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name=_TOOL_NAME, arguments={}),
                )
            )
            native = response.root.model_dump(exclude_none=True)
        assert native["isError"] is upstream_error
        assert [block["type"] for block in native["content"]] == [
            "text",
            "image",
            "text",
            "image",
            "text",
        ]
        assert native["content"][0]["text"] == EARLY_TEXT
        assert native["content"][1]["mimeType"] == "image/png"
        assert native["content"][1]["data"] == TINY_PNG_BASE64
        assert native["content"][2]["text"] == BETWEEN_TEXT
        assert native["content"][3]["mimeType"] == "image/jpeg"
        assert native["content"][3]["data"] == TINY_JPEG_BASE64
        assert native["content"][4]["text"] == LATE_TEXT
        return

    native = _dynamic_tool_result_payload(envelope)
    assert native == {
        "success": not upstream_error,
        "contentItems": [
            {"type": "inputText", "text": EARLY_TEXT},
            {"type": "inputImage", "imageUrl": f"data:image/png;base64,{TINY_PNG_BASE64}"},
            {"type": "inputText", "text": BETWEEN_TEXT},
            {"type": "inputImage", "imageUrl": f"data:image/jpeg;base64,{TINY_JPEG_BASE64}"},
            {"type": "inputText", "text": LATE_TEXT},
        ],
    }


def test_tool_result_policy_suppresses_image_before_http_return(
    isolated_http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """The real TOOL_RESULT gate replaces image bytes with its denial notice."""
    session_id = _image_session(
        isolated_http_client,
        runner_id=live_runner_id,
        mock_url=mock_llm_server_url,
        deny_result=True,
    )
    result = _call_snapshot(isolated_http_client, session_id)
    assert result == {
        "content": [{"type": "text", "text": f"[Result suppressed by policy: {_DENY_REASON}]"}]
    }
    assert TINY_PNG_BASE64 not in json.dumps(result)
    assert TINY_JPEG_BASE64 not in json.dumps(result)
    assert BETWEEN_TEXT not in json.dumps(result)
    assert LATE_TEXT not in json.dumps(result)


def test_tool_result_policy_transforms_image_before_http_return(
    isolated_http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A TOOL_RESULT transform replaces the native image-bearing result."""
    session_id = _image_session(
        isolated_http_client,
        runner_id=live_runner_id,
        mock_url=mock_llm_server_url,
        transform_result=True,
    )
    result = _call_snapshot(isolated_http_client, session_id)
    assert result == {"content": [{"type": "text", "text": TRANSFORMED_TEXT}]}
    assert TINY_PNG_BASE64 not in json.dumps(result)
    assert TINY_JPEG_BASE64 not in json.dumps(result)
    assert BETWEEN_TEXT not in json.dumps(result)
    assert LATE_TEXT not in json.dumps(result)
