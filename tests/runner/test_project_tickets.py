import json

import httpx
import pytest

from omnigent.entities.project_ticket import default_ticket_stages
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.project_tickets import execute_ticket_tool
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS

SOURCE = "a" * 32
PROJECT = "b" * 32
TICKET = "c" * 32


@pytest.mark.parametrize(
    "name,args,method,suffix,expected",
    [
        ("sys_ticket_list", {"stage_id": "open", "limit": 2}, "GET", "", None),
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
        (
            "sys_session_link_ticket",
            {"ticket_id": TICKET, "session_id": PROJECT, "involvement": " Plan the work "},
            "POST",
            f"/{TICKET}/sessions",
            {"session_id": PROJECT, "action": "attach", "involvement": "Plan the work"},
        ),
        (
            "sys_session_link_ticket",
            {"ticket_id": TICKET, "involvement": ""},
            "POST",
            f"/{TICKET}/sessions",
            {"session_id": SOURCE, "action": "attach", "involvement": ""},
        ),
        (
            "sys_ticket_link",
            {"ticket_id": TICKET, "related_ticket_id": SOURCE},
            "POST",
            f"/{TICKET}/related",
            {"related_ticket_id": SOURCE, "action": "attach"},
        ),
        (
            "sys_ticket_update",
            {"ticket_id": TICKET, "lead_session_id": None},
            "PATCH",
            f"/{TICKET}",
            {"lead_session_id": None},
        ),
        (
            "sys_ticket_update",
            {
                "ticket_id": TICKET,
                "stage_id": "planning",
                "plan": "Agreed scope",
                "lead_session_id": SOURCE,
            },
            "PATCH",
            f"/{TICKET}",
            {"stage_id": "planning", "plan": "Agreed scope", "lead_session_id": SOURCE},
        ),
        (
            "sys_ticket_update",
            {"ticket_id": TICKET, "stage_id": "review", "review_summary": "Check the demo"},
            "PATCH",
            f"/{TICKET}",
            {"stage_id": "review", "review_summary": "Check the demo"},
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
            assert request.url.params["stage_id"] == "open" and request.url.params["limit"] == "2"
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert json.loads(await execute_ticket_tool(name, args, client, SOURCE)) == {"ok": True}
    assert len(requests) == 2


async def test_get_returns_paged_activity_outputs_and_sessions():
    stages = default_ticket_stages()
    stages.stages[0].color = "purple"
    stages.stages[0].icon = "search"

    def handler(request):
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        if request.url.path.endswith("/stages"):
            return httpx.Response(200, json=stages.model_dump())
        if request.url.path.endswith("/events"):
            assert request.url.params["after"] == "6" and request.url.params["kind"] == "output"
            return httpx.Response(200, json={"data": [], "has_more": False, "last_position": 6})
        if request.url.path.endswith("/sessions"):
            return httpx.Response(
                200, json={"data": [{"session_id": SOURCE, "involvement": "Plan the work"}]}
            )
        if request.url.path.endswith("/related"):
            assert request.url.params["after"] == SOURCE
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json={"id": TICKET})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        result = json.loads(
            await execute_ticket_tool(
                "sys_ticket_get",
                {"ticket_id": TICKET, "after": 6, "kind": "output", "related_after": SOURCE},
                client,
                SOURCE,
            )
        )
    assert result["ticket"]["id"] == TICKET
    assert result["events"]["last_position"] == 6
    assert result["sessions"]["data"] == [{"session_id": SOURCE, "involvement": "Plan the work"}]
    assert result["related"] == {"data": [], "has_more": False}
    assert result["stages"] == stages.model_dump()["stages"]
    assert result["initial_stage_id"] == "open"


@pytest.mark.parametrize(
    "name,args",
    [
        ("sys_ticket_create", {"title": " ", "project_id": PROJECT}),
        ("sys_ticket_update", {"ticket_id": TICKET, "stage_id": "../other"}),
        ("sys_ticket_update", {"ticket_id": TICKET, "stage_id": "x" * 65}),
        ("sys_ticket_link", {"ticket_id": TICKET, "related_ticket_id": "../../other"}),
        ("sys_ticket_get", {"ticket_id": TICKET, "related_after": "../other"}),
        ("sys_ticket_get", {"ticket_id": "../../other"}),
        ("sys_ticket_list", {"limit": 101}),
        ("sys_session_link_ticket", {"ticket_id": TICKET, "action": "remove", "involvement": ""}),
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
    assert len(TICKET_TOOLS) == 7
    for tool in TICKET_TOOLS.values():
        properties = tool().get_schema()["function"]["parameters"]["properties"]
        assert not {"project_id", "notify_orchestrator", "version"} & properties.keys()
        assert "delete" not in tool.name()
