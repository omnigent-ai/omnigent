import json

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.project_tickets import execute_ticket_tool
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS

SOURCE = "a" * 32
PROJECT = "b" * 32
TICKET = "c" * 32


@pytest.mark.parametrize(
    "name,args,method,suffix,expected",
    [
        ("sys_ticket_list", {"status": "open", "limit": 2}, "GET", "", None),
        (
            "sys_ticket_create",
            {"title": "Do it"},
            "POST",
            "",
            {"title": "Do it", "description": "", "tags": []},
        ),
        (
            "sys_ticket_update",
            {"ticket_id": TICKET, "tags": [{"key": "needs-human"}]},
            "PATCH",
            f"/{TICKET}",
            {"tags": [{"key": "needs-human"}]},
        ),
        (
            "sys_ticket_comment",
            {"ticket_id": TICKET, "text": "Progress"},
            "POST",
            f"/{TICKET}/comments",
            {"text": "Progress", "kind": "comment", "outputs": []},
        ),
        (
            "sys_session_link_ticket",
            {"ticket_id": TICKET},
            "POST",
            f"/{TICKET}/sessions",
            {"session_id": SOURCE, "action": "attach"},
        ),
    ],
)
async def test_tools_use_only_the_callers_project(name, args, method, suffix, expected):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
        if request.url.path == f"/v1/sessions/{SOURCE}":
            assert request.url.params["include_items"] == "false"
            return httpx.Response(200, json={"project_id": PROJECT})
        assert (
            request.method == method
            and request.url.path == f"/v1/projects/{PROJECT}/tickets{suffix}"
        )
        if expected is not None:
            assert json.loads(request.content) == expected
        else:
            assert request.url.params["status"] == "open" and request.url.params["limit"] == "2"
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert json.loads(await execute_ticket_tool(name, args, client, SOURCE)) == {"ok": True}
    assert len(requests) == 2


async def test_get_returns_paged_activity_outputs_and_sessions():
    def handler(request):
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        if request.url.path.endswith("/events"):
            assert request.url.params["after"] == "6" and request.url.params["kind"] == "output"
            return httpx.Response(200, json={"data": [], "has_more": False, "last_position": 6})
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"id": TICKET})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        result = json.loads(
            await execute_ticket_tool(
                "sys_ticket_get",
                {"ticket_id": TICKET, "after": 6, "kind": "output"},
                client,
                SOURCE,
            )
        )
    assert result["ticket"]["id"] == TICKET
    assert result["events"]["last_position"] == 6 and result["sessions"]["data"] == []


@pytest.mark.parametrize(
    "name,args",
    [
        ("sys_ticket_create", {"title": " ", "project_id": PROJECT}),
        ("sys_ticket_update", {"ticket_id": TICKET, "status": "done"}),
        ("sys_ticket_get", {"ticket_id": "../../other"}),
        ("sys_ticket_list", {"limit": 101}),
        ("sys_ticket_comment", {"ticket_id": TICKET, "text": "Hi", "notify_orchestrator": False}),
    ],
)
async def test_invalid_tool_args_make_no_request(name, args):
    def handler(request):
        pytest.fail("invalid arguments reached the server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert "error" in json.loads(await execute_ticket_tool(name, args, client, SOURCE))


@pytest.mark.parametrize("status,project_id", [(200, None), (403, PROJECT)])
async def test_source_errors_are_reported_without_writes(status, project_id):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(status, json={"project_id": project_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert "error" in json.loads(
            await execute_ticket_tool("sys_ticket_create", {"title": "Work"}, client, SOURCE)
        )


def test_tool_schemas_have_no_project_override_delete_or_notification_switch():
    assert len(TICKET_TOOLS) == 6
    for tool in TICKET_TOOLS.values():
        properties = tool().get_schema()["function"]["parameters"]["properties"]
        assert not {"project_id", "notify_orchestrator", "version"} & properties.keys()
        assert "delete" not in tool.name()
