"""Runner-side argument handling and error mapping for the project tools."""

import json

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_projects import (
    execute_project_list,
    execute_project_update,
    execute_session_update,
)

pytestmark = pytest.mark.asyncio

SOURCE = "a" * 32
FAILING = "b" * 32
FAMILY_MEMBER = "c" * 32
SUB_AGENT = "d" * 32
PARENT = "e" * 32
PROJECT_ID = "f" * 32
READ_SHARED = "0" * 32
MISSING = "1" * 31 + "2"
CHECKED_INFO = {"session_move_source_checks": True}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"project": "Q3"}, "session_id must be"),
        ({"session_id": 7, "project": "Q3"}, "session_id must be"),
        ({"session_id": SOURCE}, "nothing to update"),
        ({"session_id": SOURCE, "project": None}, "project must be a name"),
        ({"session_id": SOURCE, "project": "x" * 101}, "project must be a name"),
        ({"session_id": SOURCE, "project": "  "}, "project must be a name"),
        ({"session_id": "not-a-session", "project": "Q3"}, "invalid session ID"),
        ({"session_id": SOURCE, "title": "  "}, "title must be non-blank"),
        ({"session_id": SOURCE, "title": "x" * 201}, "title must be non-blank"),
        ({"session_id": SOURCE, "pinned": "yes"}, "pinned must be true or false"),
        ({"session_id": SOURCE, "archived": 1}, "archived must be true or false"),
        ({"session_id": SOURCE, "archived": True, "pinned": True}, "cannot be pinned"),
        ({"session_id": SOURCE, "archived": True}, "cannot archive itself"),
    ],
)
async def test_update_rejects_invalid_arguments_before_any_request(args, message):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with _client(handler) as client:
        result = json.loads(await execute_session_update(args, client, SOURCE))
    assert message in result["error"]
    assert requests == []


@pytest.mark.parametrize("info", [{}, {"session_move_source_checks": False}, None])
async def test_update_refuses_a_server_without_source_checks_before_any_write(info):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/info" and info is not None:
            return httpx.Response(200, json=info)
        return httpx.Response(404 if info is None else 200, json={})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update({"session_id": SOURCE, "project": "Q3"}, client, SOURCE)
        )
    assert "cannot check the calling session's sharing" in result["error"]
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/v1/info")]


def _session_server(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if path == "/v1/projects":
            return httpx.Response(200, json={"data": [{"id": PROJECT_ID, "name": "Q3"}]})
        if request.method == "GET" and path.endswith(FAILING):
            raise httpx.ConnectError("connection reset")
        if request.method == "GET" and path.endswith(SUB_AGENT):
            return httpx.Response(200, json={"id": SUB_AGENT, "parent_session_id": PARENT})
        if request.method == "GET" and path.endswith(FAMILY_MEMBER):
            family = {"id": FAMILY_MEMBER, "family": {"representative_id": PARENT}}
            return httpx.Response(200, json=family)
        if request.method == "GET" and path.endswith(READ_SHARED):
            # Already in the target project, so only the owner check keeps it from "unchanged".
            shared = {"id": READ_SHARED, "project_id": PROJECT_ID, "permission_level": 1}
            return httpx.Response(200, json=shared)
        if request.method == "GET" and path.endswith(MISSING):
            return httpx.Response(404, json={"detail": "Session not found"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "labels": {}})
        return httpx.Response(200, json={})

    return handler


@pytest.mark.parametrize(
    ("session_id", "error"),
    [
        (FAILING, "session could not be read: connection reset"),
        (SUB_AGENT, f"sub-agent sessions follow their parent's project; update {PARENT} instead"),
        (FAMILY_MEMBER, f"this session shares its family's project; update {PARENT} instead"),
        (READ_SHARED, "only the session's owner can change its project"),
        (MISSING, "session not found or not visible to you (Session not found)"),
    ],
)
async def test_update_refuses_a_session_it_cannot_refile_without_any_write(session_id, error):
    requests: list[httpx.Request] = []
    async with _client(_session_server(requests)) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": session_id, "project": "New"}, client, SOURCE
            )
        )
    assert result == {"session_id": session_id, "error": error}
    assert not [r for r in requests if r.url.path == "/v1/projects" or r.method != "GET"]


async def test_update_files_the_session_and_sends_the_source_header():
    requests: list[httpx.Request] = []
    hyphenated_source = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    async with _client(_session_server(requests)) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": hyphenated_source, "project": " Q3 "}, client, SOURCE
            )
        )
    assert result == {
        "session_id": hyphenated_source,
        "status": "updated",
        "project": "Q3",
        "project_created": False,
    }
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert patched.url.path == f"/v1/sessions/{SOURCE}"
    assert patched.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    assert json.loads(patched.content) == {
        "project_id": PROJECT_ID,
        "labels": {"omni_project": ""},
    }


async def test_update_maps_a_family_refusal_from_the_server():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if request.method == "GET":
            return httpx.Response(200, json={"id": SOURCE, "project_id": PROJECT_ID})
        error = {"code": "conflict", "message": "Only the family representative can move"}
        return httpx.Response(409, json={"error": error})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update({"session_id": SOURCE, "project": ""}, client, SOURCE)
        )
    assert "update the moves_with session instead" in result["error"]


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


ORGANIZE_INFO = {"session_move_source_checks": True, "session_organize_source_checks": True}


@pytest.mark.parametrize(
    ("execute", "args"),
    [
        (execute_session_update, {"session_id": PARENT, "title": "New"}),
        (execute_project_update, {"project": "Q3", "name": "New"}),
    ],
    ids=["session-title", "project-rename"],
)
async def test_organizing_writes_need_the_organize_source_check(execute, args):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=CHECKED_INFO)

    async with _client(handler) as client:
        result = json.loads(await execute(args, client, SOURCE))
    assert "cannot check the calling session's sharing" in result["error"]
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/v1/info")]


def _organize_server(requests: list[httpx.Request], snapshot: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=ORGANIZE_INFO)
        if request.method == "GET":
            return httpx.Response(200, json=snapshot)
        return httpx.Response(200, json={})

    return handler


async def test_update_renames_pins_and_archives_in_one_patch_with_the_source_header():
    requests: list[httpx.Request] = []
    snapshot = {"id": PARENT, "title": "Old", "labels": {}}
    async with _client(_organize_server(requests, snapshot)) as client:
        renamed = json.loads(
            await execute_session_update(
                {"session_id": PARENT, "title": " Release notes ", "pinned": True},
                client,
                SOURCE,
            )
        )
        archived = json.loads(
            await execute_session_update({"session_id": PARENT, "archived": True}, client, SOURCE)
        )
    assert renamed == {
        "session_id": PARENT,
        "status": "updated",
        "title": "Release notes",
        "pinned": True,
    }
    assert archived == {"session_id": PARENT, "status": "updated", "archived": True}
    first, second = [r for r in requests if r.method == "PATCH"]
    assert first.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    body = json.loads(first.content)
    assert body["title"] == "Release notes"
    assert set(body) == {"title", "labels"} and int(body["labels"]["omnigent.pinned"]) > 0
    assert json.loads(second.content) == {"archived": True}


async def test_update_skips_the_patch_when_nothing_changes():
    requests: list[httpx.Request] = []
    snapshot = {
        "id": PARENT,
        "title": "Same",
        "archived": True,
        "labels": {"omnigent.pinned": "1700000000000"},
    }
    async with _client(_organize_server(requests, snapshot)) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": PARENT, "title": "Same", "archived": True},
                client,
                SOURCE,
            )
        )
        unpinned = json.loads(
            await execute_session_update({"session_id": PARENT, "pinned": False}, client, SOURCE)
        )
    assert result["status"] == "unchanged"
    assert unpinned["status"] == "updated"
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert json.loads(patched.content) == {"labels": {"omnigent.pinned": ""}}


async def test_update_lets_a_family_member_rename_but_not_refile():
    requests: list[httpx.Request] = []
    snapshot = {"id": FAMILY_MEMBER, "family": {"representative_id": PARENT}}
    async with _client(_organize_server(requests, snapshot)) as client:
        renamed = json.loads(
            await execute_session_update(
                {"session_id": FAMILY_MEMBER, "title": "Mine"}, client, SOURCE
            )
        )
        refiled = json.loads(
            await execute_session_update(
                {"session_id": FAMILY_MEMBER, "title": "Mine", "project": "Q3"}, client, SOURCE
            )
        )
    assert renamed["status"] == "updated"
    assert f"update {PARENT} instead" in refiled["error"]
    assert len([r for r in requests if r.method == "PATCH"]) == 1


ICON_CONFIG = {"icon": "🚀", "model": "fast"}


def _project_server(
    requests: list[httpx.Request],
    *,
    pages: list[list[dict]] | None = None,
    config: dict | None = None,
    patch_status: int = 200,
):
    """Project Q3 exists with *config*; its sessions are served across cursor *pages*."""
    project = {"id": PROJECT_ID, "name": "Q3", "config": config or {}}
    pages = pages or [[]]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json=ORGANIZE_INFO)
        if path == "/v1/projects":
            if request.method == "POST":
                return httpx.Response(200, json=project)
            return httpx.Response(200, json={"data": [project]})
        if path == "/v1/sessions":
            index = int(request.url.params.get("after") or 0)
            more = index + 1 < len(pages)
            body = {"data": pages[index], "has_more": more, "last_id": str(index + 1)}
            return httpx.Response(200, json=body)
        if request.method == "PATCH" and patch_status != 200:
            return httpx.Response(patch_status, json={"detail": "refused"})
        return httpx.Response(200, json={})

    return handler


@pytest.mark.parametrize(
    ("args", "expected", "body"),
    [
        (
            {"project": "Q3", "name": "Q3 launch", "icon": "📦"},
            {"project": "Q3 launch", "status": "updated", "previous_name": "Q3", "icon": "📦"},
            {"name": "Q3 launch", "config": {"model": "fast", "icon": "📦"}},
        ),
        (
            {"project": "Q3", "icon": ""},
            {"project": "Q3", "status": "updated", "icon": None},
            {"config": {"model": "fast"}},
        ),
    ],
    ids=["rename-and-replace-icon", "clear-icon"],
)
async def test_project_update_keeps_other_config_when_changing_the_icon(args, expected, body):
    requests: list[httpx.Request] = []
    async with _client(_project_server(requests, config=ICON_CONFIG)) as client:
        result = json.loads(await execute_project_update(args, client, SOURCE))
    assert result == expected
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert patched.url.path == f"/v1/projects/{PROJECT_ID}"
    assert patched.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    assert json.loads(patched.content) == body
    # Only a rename has label members left behind under the old name to look for.
    swept = [r for r in requests if r.url.path == "/v1/sessions"]
    assert len(swept) == (1 if "name" in args else 0)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "project must be a project name"),
        ({"project": " "}, "project must be a project name"),
        ({"project": "Q3"}, "nothing to update"),
        ({"project": "Q3", "name": ""}, "name must be non-blank"),
        ({"project": "Q3", "icon": "x" * 17}, "icon must be one emoji"),
    ],
)
async def test_project_update_rejects_invalid_arguments_before_any_request(args, message):
    requests: list[httpx.Request] = []
    async with _client(_project_server(requests, config=ICON_CONFIG)) as client:
        result = json.loads(await execute_project_update(args, client, SOURCE))
    assert message in result["error"]
    assert requests == []


async def test_project_update_maps_unknown_projects_and_name_conflicts():
    requests: list[httpx.Request] = []
    async with _client(_project_server(requests, patch_status=409)) as client:
        missing = json.loads(
            await execute_project_update({"project": "Nope", "name": "X"}, client, SOURCE)
        )
        taken = json.loads(
            await execute_project_update({"project": "Q3", "name": "Taken"}, client, SOURCE)
        )
    assert "no project named 'Nope'" in missing["error"]
    assert taken["error"] == "a project named 'Taken' already exists"


async def test_list_forwards_search_and_filters_and_marks_pinned_and_archived():
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"data": []})
        seen.append(request.url.params)
        rows = [
            {"id": PARENT, "status": "idle", "labels": {"omnigent.pinned": "1"}},
            {"id": FAMILY_MEMBER, "status": "idle", "archived": True},
        ]
        return httpx.Response(200, json={"data": rows, "has_more": False})

    async with _client(handler) as client:
        result = json.loads(
            await execute_project_list(
                {"search": " release ", "pinned": True, "include_archived": True},
                client,
                SOURCE,
            )
        )
        invalid = json.loads(await execute_project_list({"pinned": "yes"}, client, SOURCE))
    (params,) = seen
    assert params["search_query"] == "release"
    assert params["pinned"] == "true" and params["include_archived"] == "true"
    assert result["sessions"][0]["pinned"] is True and "archived" not in result["sessions"][0]
    assert result["sessions"][1]["archived"] is True and "pinned" not in result["sessions"][1]
    assert "pinned and include_archived must be true or false" in invalid["error"]


async def test_update_refuses_to_archive_a_session_on_the_callers_runner():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=ORGANIZE_INFO)
        session = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": session, "runner_id": "runner-1"})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update({"session_id": PARENT, "archived": True}, client, SOURCE)
        )
    assert "shares the calling session's runner" in result["error"]
    caller_read = [r for r in requests if r.method == "GET"][-1]
    assert caller_read.url.path == f"/v1/sessions/{SOURCE}"
    assert caller_read.url.params["include_usage"] == "false"
    assert caller_read.url.params["include_live_status"] == "false"
    assert not [r for r in requests if r.method == "PATCH"]


async def test_project_rename_refiles_members_that_belong_through_the_legacy_label():
    requests: list[httpx.Request] = []
    members = [
        {"id": PARENT, "labels": {"omni_project": "Q3"}},
        {
            "id": FAMILY_MEMBER,
            "labels": {"omni_project": "Q3"},
            "family": {"representative_id": PARENT},
        },
        {"id": SUB_AGENT, "project_id": PROJECT_ID},
    ]
    async with _client(_project_server(requests, pages=[members])) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Q4"}, client, SOURCE)
        )
    assert result == {"project": "Q4", "status": "updated", "previous_name": "Q3"}
    swept = [r for r in requests if r.url.path == "/v1/sessions"]
    assert swept[0].url.params["project"] == "Q3"
    renamed, refiled = [r for r in requests if r.method == "PATCH"]
    assert renamed.url.path == f"/v1/projects/{PROJECT_ID}"
    assert refiled.url.path == f"/v1/sessions/{PARENT}"
    assert refiled.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    assert json.loads(refiled.content) == {
        "project_id": PROJECT_ID,
        "labels": {"omni_project": ""},
    }


def _label_folder_server(requests: list[httpx.Request], *, create_status: int = 200):
    """Q3 exists only as a session label; project lists must be paged."""
    created = {"id": PROJECT_ID, "name": "", "config": {}}
    raced = {"id": PROJECT_ID, "name": "Q3", "config": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json=ORGANIZE_INFO)
        if path == "/v1/projects" and request.method == "GET":
            assert request.url.params.get("limit"), "project lists must be paged"
            if create_status == 409 and request.url.params.get("after") == "1" * 32:
                return httpx.Response(200, json={"data": [raced], "has_more": False})
            first = {"id": "1" * 32, "name": "Other"}
            more = create_status == 409 and len([r for r in requests if r.method == "POST"]) > 0
            body = {"data": [first], "has_more": more, "last_id": first["id"]}
            return httpx.Response(200, json=body)
        if path == "/v1/projects" and request.method == "POST":
            if create_status != 200:
                return httpx.Response(create_status, json={"detail": "taken"})
            body = json.loads(request.content)
            return httpx.Response(200, json={**created, **body})
        if path == "/v1/sessions":
            row = {"id": PARENT, "labels": {"omni_project": "Q3"}}
            return httpx.Response(200, json={"data": [row], "has_more": False})
        return httpx.Response(200, json={})

    return handler


async def test_project_update_promotes_a_label_only_folder_under_its_final_name():
    requests: list[httpx.Request] = []
    async with _client(_label_folder_server(requests)) as client:
        result = json.loads(
            await execute_project_update(
                {"project": "Q3", "name": "Q4", "icon": "🧪"}, client, SOURCE
            )
        )
    assert result == {"project": "Q4", "status": "updated", "previous_name": "Q3", "icon": "🧪"}
    (created,) = [r for r in requests if r.method == "POST"]
    assert json.loads(created.content) == {"name": "Q4", "config": {"icon": "🧪"}}
    assert created.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    assert [r.url.path for r in requests if r.method == "PATCH"] == [f"/v1/sessions/{PARENT}"]


async def test_label_only_rename_into_a_taken_name_changes_nothing():
    requests: list[httpx.Request] = []
    async with _client(_label_folder_server(requests, create_status=409)) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Taken"}, client, SOURCE)
        )
    assert result == {"project": "Q3", "error": "a project named 'Taken' already exists"}
    assert [r.method for r in requests if r.method not in ("GET",)] == ["POST"]


async def test_label_only_promotion_reuses_a_concurrent_promotion_found_on_a_later_page():
    requests: list[httpx.Request] = []
    async with _client(_label_folder_server(requests, create_status=409)) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "icon": "🧪"}, client, SOURCE)
        )
    assert result == {"project": "Q3", "status": "updated", "icon": "🧪"}
    lookups = [r.url.params.get("after") for r in requests if r.url.path == "/v1/projects"]
    assert lookups[-2:] == [None, "1" * 32]
    patched = [(r.url.path, json.loads(r.content)) for r in requests if r.method == "PATCH"]
    assert patched == [
        (f"/v1/projects/{PROJECT_ID}", {"config": {"icon": "🧪"}}),
        (f"/v1/sessions/{PARENT}", {"project_id": PROJECT_ID, "labels": {"omni_project": ""}}),
    ]


async def test_project_rename_reports_members_it_could_not_move_across_pages():
    requests: list[httpx.Request] = []
    pages = [
        [],
        [
            {"id": PARENT, "labels": {"omni_project": "Q3"}},
            {"id": FAILING, "labels": {"omni_project": "Q3"}},
            {"id": READ_SHARED, "labels": {"omni_project": "Q3"}},
        ],
    ]
    base = _project_server(requests, pages=pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path.endswith(FAILING):
            requests.append(request)
            raise httpx.ConnectError("connection reset")
        if request.method == "PATCH" and request.url.path.endswith(READ_SHARED):
            requests.append(request)
            return httpx.Response(403, json={"detail": "forbidden"})
        return base(request)

    async with _client(handler) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Q4"}, client, SOURCE)
        )
    assert result == {
        "project": "Q4",
        "status": "partial",
        "sessions_not_moved": [FAILING, READ_SHARED],
        "previous_name": "Q3",
    }
    assert [r.url.params.get("after") for r in requests if r.url.path == "/v1/sessions"] == [
        None,
        "1",
    ]
    patched = [r.url.path for r in requests if r.method == "PATCH"]
    assert patched == [
        f"/v1/projects/{PROJECT_ID}",
        f"/v1/sessions/{PARENT}",
        f"/v1/sessions/{FAILING}",
        f"/v1/sessions/{READ_SHARED}",
    ]


async def test_project_rename_warns_when_the_member_walk_hits_its_page_cap(monkeypatch):
    from omnigent.runner import session_projects

    monkeypatch.setattr(session_projects, "_MAX_LOOKUP_PAGES", 2)
    requests: list[httpx.Request] = []
    async with _client(_project_server(requests, pages=[[], [], [], []])) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Q4"}, client, SOURCE)
        )
    assert result["status"] == "partial"
    assert result["warning"].startswith("the project has too many sessions to check them all;")
    assert "sys_project_list project='Q3'" in result["warning"]
    assert len([r for r in requests if r.url.path == "/v1/sessions"]) == 2


async def test_update_sends_title_archive_and_an_existing_project_in_one_patch():
    requests: list[httpx.Request] = []
    snapshot = {"id": PARENT, "title": "Old", "labels": {}}
    base = _organize_server(requests, snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": PROJECT_ID, "name": "Q3"}]})
        return base(request)

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": PARENT, "title": "Done", "archived": True, "project": "Q3"},
                client,
                SOURCE,
            )
        )
    assert result == {
        "session_id": PARENT,
        "status": "updated",
        "project": "Q3",
        "project_created": False,
        "title": "Done",
        "archived": True,
    }
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert patched.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    assert json.loads(patched.content) == {
        "project_id": PROJECT_ID,
        "title": "Done",
        "archived": True,
        "labels": {"omni_project": ""},
    }
    assert not [r for r in requests if r.method == "POST"]


@pytest.mark.parametrize("failure", ["http-503", "page-cap"])
async def test_project_update_writes_nothing_when_project_discovery_fails(failure, monkeypatch):
    from omnigent.runner import session_projects

    monkeypatch.setattr(session_projects, "_MAX_LOOKUP_PAGES", 2)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json=ORGANIZE_INFO)
        if path == "/v1/projects" and request.method == "GET":
            if failure == "http-503":
                return httpx.Response(503, text="unavailable")
            page = {"data": [{"id": "1" * 32, "name": "Other"}], "has_more": True}
            return httpx.Response(200, json={**page, "last_id": "1" * 32})
        if path == "/v1/sessions":
            row = {"id": PARENT, "labels": {"omni_project": "Q3"}}
            return httpx.Response(200, json={"data": [row], "has_more": False})
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Q4"}, client, SOURCE)
        )
    assert "could not list projects" in result["error"]
    assert [r.method for r in requests if r.method != "GET"] == []


async def test_archive_never_recreates_a_project_deleted_after_its_preflight():
    requests: list[httpx.Request] = []
    snapshot = {"id": PARENT, "labels": {}}
    base = _organize_server(requests, snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": PROJECT_ID, "name": "Q3"}]})
        if request.method == "PATCH":
            requests.append(request)
            error = {"code": "not_found", "message": "Project not found"}
            return httpx.Response(404, json={"error": error})
        return base(request)

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": PARENT, "archived": True, "project": "Q3"}, client, SOURCE
            )
        )
    assert result["error"] == "the project no longer exists; nothing was changed"
    assert [r.url.path for r in requests if r.url.path == "/v1/projects"] == ["/v1/projects"]
    assert not [r for r in requests if r.method == "POST"]
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert json.loads(patched.content)["project_id"] == PROJECT_ID


@pytest.mark.parametrize("failure", ["http-500", "transport"])
async def test_update_refuses_to_archive_when_the_caller_cannot_be_read(failure):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=ORGANIZE_INFO)
        if request.url.path.endswith(SOURCE):
            if failure == "transport":
                raise httpx.ConnectError("connection reset")
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"id": PARENT, "runner_id": "runner-2"})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update({"session_id": PARENT, "archived": True}, client, SOURCE)
        )
    assert "shares the calling session's runner" in result["error"]
    assert not [r for r in requests if r.method == "PATCH"]


async def test_update_restores_an_archived_session():
    requests: list[httpx.Request] = []
    snapshot = {"id": PARENT, "archived": True, "labels": {}}
    async with _client(_organize_server(requests, snapshot)) as client:
        result = json.loads(
            await execute_session_update({"session_id": PARENT, "archived": False}, client, SOURCE)
        )
    assert result == {"session_id": PARENT, "status": "updated", "archived": False}
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert json.loads(patched.content) == {"archived": False}
    assert patched.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE


async def test_update_reports_a_project_it_created_before_a_refused_patch():
    requests: list[httpx.Request] = []
    snapshot = {"id": PARENT, "title": "Old", "labels": {}}
    base = _organize_server(requests, snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"id": PROJECT_ID, "name": "New"})
            return httpx.Response(200, json={"data": []})
        if request.method == "PATCH":
            requests.append(request)
            return httpx.Response(400, json={"error": {"message": "title rejected"}})
        return base(request)

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": PARENT, "title": "Bad", "project": "New"}, client, SOURCE
            )
        )
    assert result == {
        "session_id": PARENT,
        "error": "HTTP 400: title rejected",
        "project": "New",
        "project_created": True,
    }


async def test_project_rename_reports_a_member_check_that_fails_after_the_rename():
    requests: list[httpx.Request] = []
    base = _project_server(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            requests.append(request)
            return httpx.Response(503, text="unavailable")
        return base(request)

    async with _client(handler) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Q4"}, client, SOURCE)
        )
    assert result == {
        "project": "Q4",
        "status": "partial",
        "error": "project updated, but its sessions could not be checked: "
        "could not list the project's sessions: HTTP 503",
    }
    assert [r.url.path for r in requests if r.method == "PATCH"] == [f"/v1/projects/{PROJECT_ID}"]


async def test_project_rename_does_not_move_sessions_from_a_reused_old_name():
    requests: list[httpx.Request] = []
    other_project = "9" * 32
    # After the rename, "Q3" names another project, and the listing returns its sessions.
    members = [
        {"id": FAMILY_MEMBER, "project_id": other_project, "labels": {}},
        {"id": SUB_AGENT, "project_id": other_project, "labels": {"omni_project": "Q3"}},
        {"id": PARENT, "labels": {"omni_project": "Q3"}},
    ]
    async with _client(_project_server(requests, pages=[members])) as client:
        result = json.loads(
            await execute_project_update({"project": "Q3", "name": "Q4"}, client, SOURCE)
        )
    assert result == {"project": "Q4", "status": "updated", "previous_name": "Q3"}
    assert [r.url.path for r in requests if r.method == "PATCH"] == [
        f"/v1/projects/{PROJECT_ID}",
        f"/v1/sessions/{PARENT}",
    ]
