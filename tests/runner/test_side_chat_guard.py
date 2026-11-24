from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.project_orchestrator import append_project_instructions
from omnigent.runner.side_chat_guard import (
    guard_role_side_chat_tool,
    record_session_snapshot,
    role_side_chat_refusal,
)
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec, ToolGroupsConfig

PARENT = "0123456789abcdef0123456789abcdef"
ROLE_SIDE_CHAT = {"side_chat_parent_id": PARENT, "project_side_chat_role": {"name": "Lead"}}


@pytest.mark.parametrize(
    ("tool_name", "args", "refused"),
    [
        ("sys_ticket_land", {"ticket_id": "t", "sha": "a" * 40}, True),
        ("sys_session_create", {"agent_id": "a"}, True),
        ("sys_project_identity_session", {"identity_id": "i"}, True),
        ("sys_ticket_create", {"title": "T"}, True),
        ("sys_ticket_update", {"ticket_id": "t", "stage_id": "done"}, True),
        ("sys_ticket_update", {"ticket_id": "t", "labels": []}, True),
        ("sys_ticket_update", {"ticket_id": "t", "comment": "c", "audience": "human"}, False),
        ("sys_session_message", {"session_id": "worker", "message": "m"}, True),
        ("sys_session_message", {"session_id": "01234567-89ab-cdef-0123-456789ABCDEF"}, False),
        ("sys_session_message", {"session_id": "lead", "message": "m"}, False),
        ("sys_ticket_comment", {"ticket_id": "t", "text": "c"}, False),
        ("sys_ticket_list", {}, False),
    ],
)
def test_a_role_side_chat_reads_and_comments_but_cannot_coordinate(tool_name, args, refused):
    refusal = role_side_chat_refusal(tool_name, args, PARENT, "Lead")
    assert (refusal is not None) is refused
    if refusal:
        assert "side chat of the project's Lead" in refusal and PARENT in refusal


async def test_only_recorded_role_side_chats_are_refused_without_a_server_read():
    record_session_snapshot("side", ROLE_SIDE_CHAT)
    record_session_snapshot("plain-side", {"side_chat_parent_id": PARENT})
    record_session_snapshot("main", {"side_chat_parent_id": None})
    land = {"ticket_id": "t", "sha": "a" * 40}
    reads = []

    def respond(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.path)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server.test"
    ) as client:
        output = await execute_tool(
            tool_name="sys_ticket_land",
            arguments=json.dumps(land),
            server_client=client,
            conversation_id="side",
        )
    assert "side chat of the project's Lead" in json.loads(output)["error"]
    assert reads == []
    assert guard_role_side_chat_tool("sys_ticket_land", land, "side") is not None
    assert guard_role_side_chat_tool("sys_ticket_list", {}, "side") is None
    for session_id in ("plain-side", "main", "never-seen", None):
        assert guard_role_side_chat_tool("sys_ticket_land", land, session_id) is None
    # A role change on a later turn is picked up from the next snapshot.
    record_session_snapshot("side", {"side_chat_parent_id": PARENT})
    assert guard_role_side_chat_tool("sys_ticket_land", land, "side") is None


async def test_composing_instructions_records_the_role_side_chat():
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=True))
    snapshot = {**ROLE_SIDE_CHAT, "project_id": "project"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        prompt = await append_project_instructions("Author", client, "composed", spec)
    assert "## Side chat of the project's Lead" in prompt
    assert prompt.index("## Side chat of the project's Lead") < prompt.index("## Side chat\n")
    assert guard_role_side_chat_tool("sys_ticket_create", {"title": "T"}, "composed")
