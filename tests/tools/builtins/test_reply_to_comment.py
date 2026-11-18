"""Comment-thread tool storage and native-runner transports."""

import json

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import _execute_comment_tool
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.reply_to_comment import ReplyToCommentTool


def test_reply_tool_and_list_context(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqlAlchemyCommentStore(db_uri)
    monkeypatch.setattr("omnigent.runtime.get_comment_store", lambda: store)
    parent = store.add("1" * 32, "file.py", "Why?", 0, 1)
    ctx = ToolContext(
        task_id="task-test", agent_id="ag-test", conversation_id=parent.conversation_id
    )
    tool = ReplyToCommentTool()
    for index in range(21):
        output = json.loads(
            tool.invoke(json.dumps({"comment_id": parent.id, "body": f"Answer {index}"}), ctx)
        )
        assert output["reply"]["author_kind"] == "agent" and output["reply"]["created_by"] is None
    listed = json.loads(ListCommentsTool().invoke("{}", ctx))["comments"][0]
    assert listed["reply_count"] == 21
    assert len(listed["replies"]) == 20
    assert listed["replies"][0]["body"] == "Answer 1"
    assert listed["replies"][-1]["body"] == "Answer 20"
    other = ToolContext(task_id="task-test", agent_id="ag-test", conversation_id="2" * 32)
    assert "error" in json.loads(
        tool.invoke(json.dumps({"comment_id": parent.id, "body": "Wrong"}), other)
    )
    for args in ("[", "[]", "{}", json.dumps({"comment_id": parent.id, "body": " "})):
        assert "error" in json.loads(tool.invoke(args, ctx))
    no_session = ToolContext(task_id="task-test", agent_id="ag-test")
    assert "error" in json.loads(tool.invoke("{}", no_session))


@pytest.mark.asyncio
async def test_runner_comment_reply_transport() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "3" * 32, "author_kind": "agent", "created_by": None}
            )
        if request.url.path.endswith("/replies"):
            return httpx.Response(
                200,
                json={
                    "replies": [
                        {"author_kind": "agent", "body": f"Reply {index}", "created_at": index}
                        for index in range(21)
                    ],
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json=[{"id": "2" * 32, "status": "draft", "reply_count": 21}])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        reply = await _execute_comment_tool(
            "reply_to_comment",
            json.dumps({"comment_id": "2" * 32, "body": "Answer"}),
            conversation_id="1" * 32,
            server_client=client,
        )
        assert json.loads(reply)["reply"]["author_kind"] == "agent"
        assert calls[0].url.path.endswith("/replies/agent")
        assert calls[0].headers[ORCHESTRATION_SOURCE_HEADER] == "1" * 32
        listed = await _execute_comment_tool(
            "list_comments", "{}", conversation_id="1" * 32, server_client=client
        )
        replies = json.loads(listed)["comments"][0]["replies"]
        assert len(replies) == 20 and replies[0]["body"] == "Reply 1"
