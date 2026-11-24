import json

import httpx
import pytest

from omnigent.entities.project_ticket import default_ticket_stages
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.project_tickets import execute_ticket_tool
from omnigent.tools.builtins.project_tickets import READ_ONLY_TICKET_TOOLS, TICKET_TOOLS

SOURCE = "a" * 32
PROJECT = "b" * 32
TICKET = "c" * 32


@pytest.mark.parametrize(
    "name,args,method,suffix,expected",
    [
        ("sys_ticket_list", {"stage_id": "open", "limit": 2, "compact": False}, "GET", "", None),
        (
            "sys_ticket_create",
            {"title": "Do it"},
            "POST",
            "",
            {"title": "Do it", "description": "", "labels": []},
        ),
        (
            "sys_ticket_update",
            {"ticket_id": TICKET, "labels": [{"key": "needs-human"}]},
            "PATCH",
            f"/{TICKET}",
            {"labels": [{"key": "needs-human"}], "final": False},
        ),
        (
            "sys_ticket_comment",
            {"ticket_id": TICKET, "text": "Progress"},
            "POST",
            f"/{TICKET}/comments",
            {"text": "Progress", "kind": "comment", "outputs": [], "final": False},
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
            {
                "ticket_id": TICKET,
                "stage_id": "planning",
                "description": "Agreed scope",
            },
            "PATCH",
            f"/{TICKET}",
            {"stage_id": "planning", "description": "Agreed scope", "final": False},
        ),
        (
            "sys_ticket_update",
            {"ticket_id": TICKET, "stage_id": "review", "comment": "Check the demo"},
            "PATCH",
            f"/{TICKET}",
            {"stage_id": "review", "comment": "Check the demo", "final": False},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "inbox", "name": " Inbox "}]},
            "PUT",
            "/stages",
            {"stages": [{"id": "inbox", "name": "Inbox", "instructions": ""}], "moves": {}},
        ),
        (
            "sys_ticket_stages_update",
            {
                "stages": [
                    {"id": "inbox", "name": "Inbox", "color": None, "icon": None},
                    {
                        "id": "resolved",
                        "name": "Resolved",
                        "instructions": " Attach evidence ",
                        "color": "green",
                        "icon": "circle-check",
                    },
                ],
                "moves": {"open": "inbox", "done": "resolved"},
            },
            "PUT",
            "/stages",
            {
                "stages": [
                    {"id": "inbox", "name": "Inbox", "instructions": ""},
                    {
                        "id": "resolved",
                        "name": "Resolved",
                        "instructions": "Attach evidence",
                        "color": "green",
                        "icon": "circle-check",
                    },
                ],
                "moves": {"open": "inbox", "done": "resolved"},
            },
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
            assert "compact" not in request.url.params
            assert "include_guidance" not in request.url.params
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


async def test_list_is_compact_by_default_and_get_can_skip_description_and_stages():
    stages = default_ticket_stages()
    seen = []
    ticket = {
        "id": TICKET,
        "title": "Trim",
        "description": "Long description " * 50,
        "stage_id": "open",
        "labels": [{"key": "needs-human"}],
        "latest_update": "x" * 400,
        "updated_at": 1,
        "created_at": 0,
        "comment_count": 2,
        "has_running_agents": True,
        "needs_final_update": False,
        "agents": [{"agent_name": "a", "harness": "h"}],
    }

    def handler(request):
        seen.append(request.url)
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        if request.url.path.endswith("/stages"):
            return httpx.Response(200, json=stages.model_dump())
        if request.url.path.endswith("/events"):
            assert set(request.url.params) <= {"after", "kind", "limit"}
            event = {"kind": "created", "payload": {"title": "Trim", "description": "Long"}}
            return httpx.Response(200, json={"data": [event], "has_more": False})
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/related"):
            return httpx.Response(200, json={"data": [{"id": SOURCE, "description": "Long"}]})
        if request.url.path.endswith(f"/tickets/{TICKET}"):
            assert request.url.params.get("include_guidance") == "true"
            return httpx.Response(200, json={**ticket, "agent_guidance": "G"})
        assert "include_guidance" not in request.url.params
        return httpx.Response(
            200,
            json={**stages.model_dump(), "data": [ticket], "has_more": False, "last_id": TICKET},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        listed = json.loads(await execute_ticket_tool("sys_ticket_list", {}, client, SOURCE))
        detail = json.loads(
            await execute_ticket_tool(
                "sys_ticket_get",
                {
                    "ticket_id": TICKET,
                    "include_description": False,
                    "include_stages": False,
                    "include_guidance": True,
                },
                client,
                SOURCE,
            )
        )
    row = listed["data"][0]
    assert set(row) == {
        "id",
        "title",
        "stage_id",
        "labels",
        "latest_update",
        "updated_at",
        "comment_count",
        "has_running_agents",
        "needs_final_update",
    }
    assert len(row["latest_update"]) == 160 and row["latest_update"].endswith("…")
    assert listed["stages"][0] == {"id": "open", "name": "Open", "is_complete": False}
    assert listed["initial_stage_id"] == "open" and listed["last_id"] == TICKET
    assert "description" not in detail["ticket"]
    assert detail["ticket"]["agent_guidance"] == "G"
    assert "description" not in detail["events"]["data"][0]["payload"]
    assert "description" not in detail["related"]["data"][0]
    assert "stages" not in detail
    assert not any(url.path.endswith("/stages") for url in seen)


@pytest.mark.parametrize(
    "name,args",
    [
        ("sys_ticket_create", {"title": " ", "project_id": PROJECT}),
        ("sys_ticket_create", {"title": "Work", "tags": []}),
        ("sys_ticket_create", {"title": "Work", "tags": None}),
        ("sys_ticket_create", {"title": "Work", "tags": [{"key": "needs-human"}]}),
        ("sys_ticket_update", {"ticket_id": TICKET, "tags": []}),
        ("sys_ticket_update", {"ticket_id": TICKET, "tags": None}),
        ("sys_ticket_update", {"ticket_id": TICKET, "tags": [{"key": "needs-human"}]}),
        ("sys_ticket_update", {"ticket_id": TICKET, "stage_id": "../other"}),
        ("sys_ticket_update", {"ticket_id": TICKET, "stage_id": "x" * 65}),
        ("sys_ticket_update", {"ticket_id": TICKET, "plan": "Old approach"}),
        ("sys_ticket_update", {"ticket_id": TICKET, "review_summary": "Old review"}),
        ("sys_ticket_update", {"ticket_id": TICKET, "completion_summary": "Old result"}),
        ("sys_ticket_update", {"ticket_id": TICKET, "review_action": "approve"}),
        ("sys_ticket_update", {"ticket_id": TICKET, "lead_session_id": SOURCE}),
        ("sys_ticket_update", {"ticket_id": TICKET, "lead_session_id": None}),
        ("sys_ticket_link", {"ticket_id": TICKET, "related_ticket_id": "../../other"}),
        ("sys_ticket_get", {"ticket_id": TICKET, "related_after": "../other"}),
        ("sys_ticket_get", {"ticket_id": "../../other"}),
        ("sys_ticket_list", {"limit": 101}),
        ("sys_session_link_ticket", {"ticket_id": TICKET, "action": "remove", "involvement": ""}),
        ("sys_ticket_comment", {"ticket_id": TICKET, "text": "Hi", "notify_orchestrator": False}),
        ("sys_ticket_stages_update", {"stages": []}),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "A"}, {"id": "open", "name": "B"}]},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": " New "}, {"id": "other", "name": "new"}]},
        ),
        ("sys_ticket_stages_update", {"stages": [{"id": "open", "name": " "}]}),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "../other", "name": "New"}]},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "New", "is_complete": True}]},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "New"}], "initial_stage_id": "open"},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "New"}], "project_id": PROJECT},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "New", "icon": "unknown"}]},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "New"}], "moves": {"open": "open"}},
        ),
        (
            "sys_ticket_stages_update",
            {"stages": [{"id": "open", "name": "New"}], "moves": {"removed": "missing"}},
        ),
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
@pytest.mark.parametrize(
    "name,args",
    [
        ("sys_ticket_create", {"title": "Work"}),
        ("sys_ticket_stages_update", {"stages": [{"id": "open", "name": "New"}]}),
    ],
)
async def test_source_errors_are_reported_without_writes(status, project_id, name, args):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(status, json={"project_id": project_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert "error" in json.loads(await execute_ticket_tool(name, args, client, SOURCE))


@pytest.mark.parametrize(
    "name,args",
    [
        ("sys_ticket_list", {}),
        ("sys_ticket_get", {"ticket_id": TICKET}),
        ("sys_project_identity_list", {}),
        (
            "sys_project_file_view",
            {"url": f"/v1/projects/{PROJECT}/ticket-attachments/{TICKET}/content"},
        ),
        ("sys_project_kpis_get", {"window": "30d"}),
    ],
)
async def test_read_only_tools_only_send_gets(name, args):
    # Codex runs these without an approval prompt, so they must never write.
    assert READ_ONLY_TICKET_TOOLS == (
        "sys_ticket_list",
        "sys_ticket_get",
        "sys_project_identity_list",
        "sys_project_file_view",
        "sys_project_kpis_get",
    )
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        return httpx.Response(200, json={"data": [], "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        await execute_ticket_tool(name, args, client, SOURCE)
    assert len(methods) > 1
    assert set(methods) == {"GET"}


def test_tool_schemas_have_no_project_override_delete_or_notification_switch():
    assert len(TICKET_TOOLS) == 19
    for tool in TICKET_TOOLS.values():
        properties = tool().get_schema()["function"]["parameters"]["properties"]
        assert (
            not {
                "project_id",
                "notify_orchestrator",
                "version",
                "plan",
                "review_summary",
                "completion_summary",
                "review_action",
                "lead_session_id",
                "tags",
            }
            & properties.keys()
        )
        assert "delete" not in tool.name()
        assert "lead_session_id" not in tool.description()
        assert "response contact" not in tool.description()
        if tool.name() in ("sys_ticket_create", "sys_ticket_update"):
            assert "labels" in properties
            schema = tool().get_schema()["function"]["parameters"]
            assert "TicketLabel" in schema["$defs"] and "TicketTag" not in schema["$defs"]
    schema = TICKET_TOOLS["sys_ticket_stages_update"]().get_schema()["function"]["parameters"]
    assert set(schema["properties"]) == {"stages", "moves"}
    assert schema["required"] == ["stages"]
    assert set(schema["$defs"]["TicketStageDefinition"]["properties"]) == {
        "id",
        "name",
        "instructions",
        "color",
        "icon",
    }


async def test_mandate_proposal_goes_to_the_callers_project_and_reports_it_pending():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        assert request.method == "POST"
        assert request.url.path == f"/v1/projects/{PROJECT}/mandate/proposals"
        assert json.loads(request.content) == {"text": "Attach a video to every change."}
        proposal = {"id": TICKET, "text": "Attach a video to every change.", "created_at": 1}
        return httpx.Response(200, json={"proposal": proposal, "project": {"id": PROJECT}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server.test"
    ) as client:
        result = json.loads(
            await execute_ticket_tool(
                "sys_project_mandate_propose",
                {"text": "Attach a video to every change."},
                client,
                SOURCE,
            )
        )
    assert result["status"] == "pending_owner_approval"
    assert result["proposal"]["id"] == TICKET
    assert "Not in effect" in result["note"]
    assert len(requests) == 2
    description = TICKET_TOOLS["sys_project_mandate_propose"].description()
    assert "changes nothing until the owner accepts it" in description


async def test_identity_session_reuses_the_named_identitys_session_and_messages_it(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "omnigent.runner.session_message.peer_session_messaging_enabled", lambda: True
    )
    identity = "d" * 32
    live = "e" * 32
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
        path = request.url.path
        if path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        if path == f"/v1/projects/{PROJECT}/identities":
            roster = [
                {"id": "orchestrator", "name": "Orchestrator", "builtin": True},
                {
                    "id": identity,
                    "name": "PM",
                    "builtin": False,
                    "active_session_id": live,
                    "active_session_state": "idle",
                },
            ]
            return httpx.Response(200, json={"data": roster})
        if path == f"/v1/projects/{PROJECT}/identities/{identity}/session":
            assert json.loads(request.content) == {"expected_session_id": live}
            return httpx.Response(200, json={"session_id": live, "created": False})
        assert path == f"/v1/sessions/{live}/events"
        assert json.loads(request.content)["data"] == {"message": "Own ticket X."}
        return httpx.Response(200, json={"queued": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server.test"
    ) as client:
        result = json.loads(
            await execute_ticket_tool(
                "sys_project_identity_session",
                {"identity": "pm", "message": "Own ticket X."},
                client,
                SOURCE,
            )
        )
        missing = json.loads(
            await execute_ticket_tool(
                "sys_project_identity_session", {"identity": "Orchestrator"}, client, SOURCE
            )
        )
    assert result == {
        "identity": {"id": identity, "name": "PM"},
        "session_id": live,
        "created": False,
        "previous_state": "idle",
        "message_delivery": {"queued": True},
    }
    assert "not a project identity" in missing["error"]
    assert ("POST", f"/v1/sessions/{live}/events") in requests


class _FakeTeam:
    """A project's identity routes: unique names, one session each, injectable faults."""

    def __init__(self, roles=()):
        self.roles = [dict(role) for role in roles]
        self.inserts = 0
        self.session_failures = 0

    def handler(self, request):
        path = request.url.path
        base = f"/v1/projects/{PROJECT}/identities"
        if path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        if path == base and request.method == "GET":
            lead = {"id": "orchestrator", "name": "Lead", "builtin": True}
            return httpx.Response(200, json={"data": [lead, *self.roles]})
        if path == base and request.method == "POST":
            body = json.loads(request.content)
            if any(role["name"].casefold() == body["name"].casefold() for role in self.roles):
                return httpx.Response(409, json={"error": {"code": "already_exists"}})
            self.inserts += 1
            role = {
                "id": f"{self.inserts:032x}",
                "name": body["name"],
                "builtin": False,
                "instructions": body["instructions"],
                "created_by_session_id": SOURCE,
                "active_session_id": f"{self.inserts + 100:032x}",
                "active_session_state": "idle",
            }
            self.roles.append(role)
            return httpx.Response(200, json=role)
        role = next(role for role in self.roles if path == f"{base}/{role['id']}/session")
        if self.session_failures:
            self.session_failures -= 1
            return httpx.Response(503, json={"error": {"message": "No host online"}})
        created = role["active_session_state"] in ("archived", "missing")
        if created:
            role["active_session_id"] = "f" * 32
            role["active_session_state"] = "idle"
        return httpx.Response(
            200, json={"session_id": role["active_session_id"], "created": created}
        )

    async def call(self, name, args):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="http://server.test"
        ) as client:
            return json.loads(await execute_ticket_tool(name, args, client, SOURCE))


_MCP_PERSONA = "You are the project's MCP Engineer. You own MCP servers."


async def test_fresh_project_staffs_a_specialist_that_the_team_list_then_shows():
    team = _FakeTeam()
    assert await team.call("sys_project_identity_list", {}) == {"data": []}
    created = await team.call(
        "sys_project_identity_create", {"name": "MCP Engineer", "instructions": _MCP_PERSONA}
    )
    assert created["existing"] is False
    [listed] = (await team.call("sys_project_identity_list", {}))["data"]
    assert listed["name"] == "MCP Engineer"
    assert listed["instructions"] == _MCP_PERSONA
    assert listed["created_by"] == "agent"
    assert listed["session_state"] == "idle"


@pytest.mark.parametrize(
    "persona,creator,differ,hint",
    [
        (_MCP_PERSONA, SOURCE, False, "matches"),
        ("You are an MCP Engineer. Different text.", SOURCE, True, "sys_project_identity_update"),
        ("You are an MCP Engineer. Different text.", None, True, "only they can edit"),
    ],
)
async def test_create_on_an_existing_name_reuses_it_and_reports_persona_drift(
    persona, creator, differ, hint
):
    # A retried or rolled-over orchestrator re-creating a role must converge on it.
    team = _FakeTeam(
        [
            {
                "id": "d" * 32,
                "name": "MCP Engineer",
                "builtin": False,
                "instructions": _MCP_PERSONA,
                "created_by_session_id": creator,
                "active_session_id": "e" * 32,
                "active_session_state": "busy",
            }
        ]
    )
    result = await team.call(
        "sys_project_identity_create", {"name": "mcp engineer", "instructions": persona}
    )
    assert team.inserts == 0
    assert len(team.roles) == 1
    assert team.roles[0]["instructions"] == _MCP_PERSONA
    assert result["existing"] is True
    assert result["instructions_differ"] is differ
    assert result["identity"]["id"] == "d" * 32
    assert result["identity"]["session_id"] == "e" * 32
    assert hint in result["note"]


async def test_concurrent_workstreams_get_separate_specialists_and_sessions():
    import asyncio

    team = _FakeTeam()
    names = ("MCP Engineer", "Docs Writer")
    await asyncio.gather(
        *(
            team.call("sys_project_identity_create", {"name": name, "instructions": f"You {name}"})
            for name in names
        )
    )
    opened = await asyncio.gather(
        *(team.call("sys_project_identity_session", {"identity": name}) for name in names)
    )
    assert {result["identity"]["name"] for result in opened} == set(names)
    assert len({result["session_id"] for result in opened}) == 2
    assert team.inserts == 2


async def test_failed_startup_is_reported_and_a_retry_resumes_the_archived_session():
    team = _FakeTeam(
        [
            {
                "id": "d" * 32,
                "name": "MCP Engineer",
                "builtin": False,
                "instructions": _MCP_PERSONA,
                "active_session_id": "e" * 32,
                "active_session_state": "archived",
            }
        ]
    )
    team.session_failures = 1
    failed = await team.call("sys_project_identity_session", {"identity": "MCP Engineer"})
    assert "HTTP 503" in failed["error"]
    resumed = await team.call("sys_project_identity_session", {"identity": "MCP Engineer"})
    assert resumed["created"] is True
    assert resumed["previous_state"] == "archived"
    assert resumed["session_id"] == "f" * 32
    assert team.inserts == 0


def _png_bytes() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 0, 100)).save(out, format="PNG")
    return out.getvalue()


def _file_view_client(content: bytes, content_type: str, seen: list):
    def handler(request):
        seen.append(
            (request.method, request.url.path, request.headers.get(ORCHESTRATION_SOURCE_HEADER))
        )
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


@pytest.mark.parametrize(
    "url",
    [
        f"/v1/projects/{PROJECT}/ticket-attachments/{TICKET}/content",
        f"http://127.0.0.1:6768/v1/projects/{PROJECT}/ticket-attachments/{TICKET}/content",
    ],
)
async def test_file_view_returns_a_ticket_screenshot_as_an_image(url):
    from omnigent.runtime.mcp_tool_result import decode_mcp_image_result

    seen: list = []
    async with _file_view_client(_png_bytes(), "image/png", seen) as client:
        output = await execute_ticket_tool("sys_project_file_view", {"url": url}, client, SOURCE)
    result = decode_mcp_image_result(json.loads(output))
    assert result is not None
    assert [block["type"] for block in result.content] == ["text", "image"]
    assert result.content[1]["mimeType"] == "image/png"
    assert seen[-1] == (
        "GET",
        f"/v1/projects/{PROJECT}/ticket-attachments/{TICKET}/content",
        SOURCE,
    )


async def test_kpi_tools_read_and_replace_the_projects_kpis():
    seen: list = []

    def handler(request):
        seen.append((request.method, request.url.path, request.url.query, request.content))
        if request.url.path == f"/v1/sessions/{SOURCE}":
            return httpx.Response(200, json={"project_id": PROJECT})
        return httpx.Response(200, json={"object": "project.kpis", "revision": 2})

    kpi = {"name": "Spend", "metric": "spend_usd", "why": "Budget.", "target": 50}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        read = await execute_ticket_tool("sys_project_kpis_get", {}, client, SOURCE)
        written = await execute_ticket_tool(
            "sys_project_kpis_set",
            {"kpis": [{**kpi, "direction": "at_most"}], "base_revision": 1},
            client,
            SOURCE,
        )
        invalid = await execute_ticket_tool(
            "sys_project_kpis_set", {"kpis": [kpi]}, client, SOURCE
        )
    assert json.loads(read)["revision"] == 2
    assert json.loads(written)["revision"] == 2
    assert "error" in json.loads(invalid)
    kpi_calls = [call for call in seen if call[1] == f"/v1/projects/{PROJECT}/kpis"]
    assert [(method, query) for method, _, query, _ in kpi_calls] == [
        ("GET", b"window=7d"),
        ("PUT", b""),
    ]
    assert json.loads(kpi_calls[1][3]) == {
        "kpis": [{**kpi, "direction": "at_most"}],
        "base_revision": 1,
    }


async def test_file_view_refuses_another_projects_file_without_fetching_it():
    seen: list = []
    other = "d" * 32
    async with _file_view_client(b"", "image/png", seen) as client:
        output = await execute_ticket_tool(
            "sys_project_file_view",
            {"url": f"/v1/projects/{other}/files/{TICKET}/content"},
            client,
            SOURCE,
        )
    assert json.loads(output) == {"error": "That file belongs to another project"}
    assert [path for _, path, _ in seen] == [f"/v1/sessions/{SOURCE}"]


async def test_file_view_reads_text_and_saves_into_the_workspace(tmp_path):
    seen: list = []
    async with _file_view_client(b"INFO ok\n", "text/plain; charset=utf-8", seen) as client:
        output = await execute_ticket_tool(
            "sys_project_file_view",
            {"url": f"/v1/projects/{PROJECT}/files/{TICKET}/content", "save_path": "out/run.log"},
            client,
            SOURCE,
            tmp_path,
        )
    result = json.loads(output)
    assert result["text"] == "INFO ok\n"
    assert (tmp_path / "out" / "run.log").read_bytes() == b"INFO ok\n"


async def test_file_view_rejects_other_paths_and_workspace_escapes(tmp_path):
    seen: list = []
    async with _file_view_client(b"x", "text/plain", seen) as client:
        bad_path = await execute_ticket_tool(
            "sys_project_file_view", {"url": "/v1/sessions/x/files/y/content"}, client, SOURCE
        )
        escape = await execute_ticket_tool(
            "sys_project_file_view",
            {"url": f"/v1/projects/{PROJECT}/files/{TICKET}/content", "save_path": "../x"},
            client,
            SOURCE,
            tmp_path,
        )
    assert "Not a project file path" in json.loads(bad_path)["error"]
    assert "error" in json.loads(escape)
    assert all(path == f"/v1/sessions/{SOURCE}" for _, path, _ in seen)
