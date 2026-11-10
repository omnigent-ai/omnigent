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


async def test_move_isolates_failures_and_sends_the_source_header():
    patched: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/projects":
            return httpx.Response(200, json={"data": [{"id": "p" * 32, "name": "Q3"}]})
        if request.method == "GET" and path.endswith(FAILING):
            raise httpx.ConnectError("connection reset")
        if request.method == "GET":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "labels": {}})
        patched.append(request)
        if path.endswith(FAMILY_MEMBER):
            error = {"code": "conflict", "message": "Only the family representative can move"}
            return httpx.Response(409, json={"error": error})
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_move(
                {"session_ids": [FAILING, FAMILY_MEMBER, SOURCE], "project": " Q3 "},
                client,
                SOURCE,
            )
        )
    statuses = [(r["status"], r.get("error", "")) for r in result["results"]]
    assert statuses[0][0] == "error" and "could not be confirmed" in statuses[0][1]
    assert statuses[1][0] == "error" and "moves with its family" in statuses[1][1]
    assert statuses[2] == ("moved", "")
    assert result["project"] == "Q3" and result["project_created"] is False
    assert all(r.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE for r in patched)
    assert json.loads(patched[-1].content) == {
        "project_id": "p" * 32,
        "labels": {"omni_project": ""},
    }


async def test_list_pages_and_names_projects():
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/projects":
            return httpx.Response(200, json=[{"id": "p" * 32, "name": "Q3"}])
        seen.append(request.url.params)
        rows = [
            {"id": SOURCE, "title": "Me", "status": "running", "project_id": "p" * 32},
            {
                "id": FAMILY_MEMBER,
                "status": "idle",
                "family": {"id": SOURCE, "representative_id": SOURCE},
            },
        ]
        return httpx.Response(200, json={"data": rows, "has_more": True, "last_id": FAMILY_MEMBER})

    async with _client(handler) as client:
        result = json.loads(
            await execute_project_list({"project": "", "limit": 500, "after": "x"}, client, SOURCE)
        )
    assert seen[0]["project"] == "" and seen[0]["limit"] == "200" and seen[0]["after"] == "x"
    assert seen[0]["visibility"] == "mine" and seen[0]["kind"] == "default"
    assert result["sessions"][0] == {
        "session_id": SOURCE,
        "title": "Me",
        "status": "running",
        "project": "Q3",
        "this_session": True,
    }
    assert result["sessions"][1]["moves_with"] == SOURCE
    assert result["has_more"] is True and result["next_cursor"] == FAMILY_MEMBER
