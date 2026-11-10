"""Runner-side argument handling and error mapping for the project tools."""

import json

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_projects import execute_project_list, execute_session_move

pytestmark = pytest.mark.asyncio

SOURCE = "a" * 32
FAILING = "b" * 32
FAMILY_MEMBER = "c" * 32
SUB_AGENT = "d" * 32
PARENT = "e" * 32
PROJECT_ID = "f" * 32
READ_SHARED = "0" * 32
CHECKED_INFO = {"session_move_source_checks": True}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"session_ids": [], "project": "Q3"}, "session_ids must list"),
        ({"session_ids": [SOURCE] * 101, "project": "Q3"}, "session_ids must list"),
        ({"session_ids": [7], "project": "Q3"}, "session_ids must list"),
        ({"session_ids": [SOURCE]}, "project must be a name"),
        ({"session_ids": [SOURCE], "project": "x" * 101}, "project must be a name"),
    ],
)
async def test_move_rejects_invalid_arguments_before_any_request(args, message):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with _client(handler) as client:
        result = json.loads(await execute_session_move(args, client, SOURCE))
    assert message in result["error"]
    assert requests == []


@pytest.mark.parametrize("info", [{}, {"session_move_source_checks": False}, None])
async def test_move_refuses_a_server_without_source_checks_before_any_write(info):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/info" and info is not None:
            return httpx.Response(200, json=info)
        return httpx.Response(404 if info is None else 200, json={})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_move({"session_ids": [SOURCE], "project": "Q3"}, client, SOURCE)
        )
    assert "cannot check the calling session's sharing" in result["error"]
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/v1/info")]


async def test_move_isolates_failures_and_sends_the_source_header():
    patched: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if path == "/v1/projects":
            return httpx.Response(200, json={"data": [{"id": PROJECT_ID, "name": "Q3"}]})
        if request.method == "GET" and path.endswith(FAILING):
            raise httpx.ConnectError("connection reset")
        if request.method == "GET" and path.endswith(SUB_AGENT):
            return httpx.Response(200, json={"id": SUB_AGENT, "parent_session_id": PARENT})
        if request.method == "GET" and path.endswith(READ_SHARED):
            # Already in the target project, so only the owner check keeps it from "unchanged".
            shared = {"id": READ_SHARED, "project_id": PROJECT_ID, "permission_level": 1}
            return httpx.Response(200, json=shared)
        if request.method == "GET":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "labels": {}})
        patched.append(request)
        if path.endswith(FAMILY_MEMBER):
            error = {"code": "conflict", "message": "Only the family representative can move"}
            return httpx.Response(409, json={"error": error})
        return httpx.Response(200, json={})

    hyphenated_source = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    async with _client(handler) as client:
        result = json.loads(
            await execute_session_move(
                {
                    "session_ids": [
                        FAILING,
                        SUB_AGENT,
                        FAMILY_MEMBER,
                        READ_SHARED,
                        SOURCE,
                        hyphenated_source,
                    ],
                    "project": " Q3 ",
                },
                client,
                SOURCE,
            )
        )
    statuses = [(r["status"], r.get("error", "")) for r in result["results"]]
    assert len(statuses) == 5, "two spellings of one session move it once"
    assert statuses[0][0] == "error" and "could not be read" in statuses[0][1]
    assert statuses[1] == (
        "error",
        f"sub-agent sessions follow their parent; move {PARENT} instead",
    )
    assert statuses[2][0] == "error" and "moves with its family" in statuses[2][1]
    assert statuses[3] == ("error", "only the session's owner can move it between projects")
    assert statuses[4] == ("moved", "")
    assert result["project"] == "Q3" and result["project_created"] is False
    assert all(r.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE for r in patched)
    assert not [r for r in patched if r.url.path.endswith(READ_SHARED)]
    assert json.loads(patched[-1].content) == {
        "project_id": PROJECT_ID,
        "labels": {"omni_project": ""},
    }


async def test_move_creates_no_project_when_no_session_can_move():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        return httpx.Response(404, json={"detail": "Session not found"})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_move(
                {"session_ids": ["not-a-session", FAILING], "project": "New"}, client, SOURCE
            )
        )
    assert [r["status"] for r in result["results"]] == ["error", "error"]
    assert result["project_created"] is False
    assert not [r for r in requests if r.url.path == "/v1/projects" or r.method != "GET"]


async def test_list_pages_projects_and_sessions_until_the_last_page():
    seen: dict[str, list[httpx.QueryParams]] = {"projects": [], "sessions": []}
    page_two = {"id": "9" * 32, "name": "Later"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/v1/projects/{PROJECT_ID}":
            return httpx.Response(200, json={"id": PROJECT_ID, "name": "Q3"})
        if path == "/v1/projects":
            seen["projects"].append(request.url.params)
            if request.url.params.get("after") == "1" * 32:
                return httpx.Response(200, json={"data": [page_two], "has_more": False})
            first = {"id": "1" * 32, "name": "First"}
            return httpx.Response(
                200, json={"data": [first], "has_more": True, "last_id": first["id"]}
            )
        seen["sessions"].append(request.url.params)
        if request.url.params.get("after") == FAMILY_MEMBER:
            return httpx.Response(200, json={"data": [], "has_more": False})
        rows = [
            {"id": SOURCE, "title": "Me", "status": "running", "project_id": PROJECT_ID},
            {
                "id": FAMILY_MEMBER,
                "status": "idle",
                "labels": {"omni_project": "Legacy folder"},
                "family": {"id": SOURCE, "representative_id": SOURCE},
            },
        ]
        return httpx.Response(200, json={"data": rows, "has_more": True, "last_id": FAMILY_MEMBER})

    async with _client(handler) as client:
        first = json.loads(
            await execute_project_list({"project": "", "limit": 500}, client, SOURCE)
        )
        last = json.loads(
            await execute_project_list(
                {
                    "after": first["next_cursor"],
                    "projects_after": first["projects_next_cursor"],
                },
                client,
                SOURCE,
            )
        )
    assert seen["sessions"][0]["project"] == "" and seen["sessions"][0]["limit"] == "200"
    assert seen["sessions"][0]["visibility"] == "mine" and seen["sessions"][0]["kind"] == "default"
    assert seen["sessions"][0]["sort_by"] == "updated_at"
    assert seen["projects"][0]["limit"] == "200" and "after" not in seen["projects"][0]
    assert first["projects"] == [{"name": "First", "id": "1" * 32}]
    assert first["sessions"][0] == {
        "session_id": SOURCE,
        "title": "Me",
        "status": "running",
        "project": "Q3",
        "this_session": True,
    }
    assert first["sessions"][1]["project"] == "Legacy folder"
    assert first["sessions"][1]["moves_with"] == SOURCE
    assert first["next_cursor"] == FAMILY_MEMBER and first["projects_next_cursor"] == "1" * 32
    assert seen["sessions"][1]["after"] == FAMILY_MEMBER
    assert seen["projects"][1]["after"] == "1" * 32
    assert last["projects"] == [page_two] and last["sessions"] == []
    assert last["next_cursor"] is None and last["projects_next_cursor"] is None
