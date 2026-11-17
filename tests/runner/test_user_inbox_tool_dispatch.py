"""Tests for the ``sys_user_inbox_*`` tools: registration and runner dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _USER_INBOX_TOOLS,
    execute_tool,
)
from omnigent.spec.parser import parse_tool_groups
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.tools.manager import ToolManager


class _Resp:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _RecordingClient:
    def __init__(self, *responses: _Resp) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"verb": "POST", "url": url, **kwargs})
        return self._responses.pop(0)

    async def get(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"verb": "GET", "url": url, **kwargs})
        return self._responses.pop(0)


def _names(spec: AgentSpec) -> set[str]:
    return {schema["function"]["name"] for schema in ToolManager(spec).get_tool_schemas()}


def test_tools_register_only_when_the_group_is_on() -> None:
    assert not _USER_INBOX_TOOLS & _names(AgentSpec(spec_version=1))
    opted_in = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(user_inbox=True))
    assert _names(opted_in) >= _USER_INBOX_TOOLS
    assert parse_tool_groups({"user_inbox": True}, spawn=False).user_inbox is True
    assert parse_tool_groups(None, spawn=False).user_inbox is False
    assert _USER_INBOX_TOOLS <= _ALL_LOCAL_TOOLS
    assert _USER_INBOX_TOOLS <= _NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_post_forwards_the_body_with_the_source_session() -> None:
    client = _RecordingClient(_Resp({"item": {"id": "i1"}, "duplicate": False}))
    arguments = {"kind": "info", "title": "t", "body_md": "b", "topic": "x"}

    out = await execute_tool(
        tool_name="sys_user_inbox_post",
        arguments=json.dumps(arguments),
        server_client=client,
        conversation_id="conv_source",
    )

    assert json.loads(out) == {"item": {"id": "i1"}, "duplicate": False}
    (call,) = client.calls
    assert (call["verb"], call["url"], call["json"]) == ("POST", "/v1/inbox/items", arguments)
    assert call["headers"] == {ORCHESTRATION_SOURCE_HEADER: "conv_source"}


@pytest.mark.parametrize(
    ("status", "message"),
    [(403, "server returned 403"), (404, "does not support Inbox items")],
    ids=["rejected", "unsupported-server"],
)
@pytest.mark.asyncio
async def test_server_errors_become_tool_errors(status: int, message: str) -> None:
    for tool_name in ("sys_user_inbox_post", "sys_user_inbox_list"):
        client = _RecordingClient(_Resp({"detail": "no"}, status_code=status))

        out = await execute_tool(
            tool_name=tool_name,
            arguments="{}",
            server_client=client,
            conversation_id="conv_source",
        )

        assert message in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_list_pages_and_trims_items() -> None:
    item = {
        "id": "i1",
        "kind": "followup",
        "title": "t",
        "topic": "x",
        "body_md": "b" * 1000,
        "dedupe_key": "v1:secret-detail",
        "status": "dismissed",
        "resolution": "already_done",
        "actions": [{"label": "Reply", "prompt": "long prompt"}],
    }
    client = _RecordingClient(
        _Resp({"data": [item], "next_cursor": "10:" + "a" * 32}),
        _Resp({"data": [{**item, "id": "i2"}], "next_cursor": None}),
    )

    out = json.loads(
        await execute_tool(
            tool_name="sys_user_inbox_list",
            arguments="{}",
            server_client=client,
            conversation_id="conv_source",
        )
    )

    assert [entry["id"] for entry in out["items"]] == ["i1", "i2"]
    assert len(out["items"][0]["body_preview"]) == 300
    assert "dedupe_key" not in out["items"][0]
    assert (out["items"][0]["status"], out["items"][0]["resolution"]) == (
        "dismissed",
        "already_done",
    )
    assert out["items"][0]["action_labels"] == ["Reply"]
    assert out["truncated"] is False
    assert client.calls[1]["params"]["after"] == "10:" + "a" * 32
    assert client.calls[0]["params"]["created_after"] > 0


@pytest.mark.asyncio
async def test_list_reports_truncation() -> None:
    page = _Resp({"data": [{"id": "i"}], "next_cursor": "10:" + "a" * 32})
    client = _RecordingClient(page, page, page)

    out = json.loads(
        await execute_tool(
            tool_name="sys_user_inbox_list",
            arguments="{}",
            server_client=client,
            conversation_id="conv_source",
        )
    )

    assert out["truncated"] is True
    assert len(client.calls) == 3
