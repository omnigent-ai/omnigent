"""sys_project_ticket_file finds the other project by name or ID before filing."""

import json

from omnigent.runner.project_tickets import _file_in_other_project

OWN, OTHER, TWIN = "a" * 32, "b" * 32, "c" * 32


def _requester(projects: list[dict]):
    calls: list[tuple[str, str, dict | None]] = []

    async def request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if method == "GET":
            return {"data": projects}
        return {"object": "project.intake_ticket", "id": "t1", "project_id": OTHER}

    return request, calls


async def test_files_on_the_project_named_case_insensitively():
    request, calls = _requester(
        [{"id": OWN, "name": "Mine"}, {"id": OTHER, "name": "Her Factory"}]
    )
    body = {"project": " her factory ", "title": "Help", "description": "Please."}
    result = json.loads(await _file_in_other_project(body, OWN, request))
    assert result["id"] == "t1"
    assert calls[-1] == (
        "POST",
        f"/v1/projects/{OTHER}/tickets/intake",
        {"title": "Help", "description": "Please."},
    )


async def test_refuses_unknown_ambiguous_and_own_projects_without_filing():
    projects = [
        {"id": OWN, "name": "Mine"},
        {"id": OTHER, "name": "Twin"},
        {"id": TWIN, "name": "Twin"},
    ]
    for wanted, error in (
        ("Nope", "No project named"),
        ("Twin", "Several projects"),
        (OWN, "own project"),
    ):
        request, calls = _requester(projects)
        result = json.loads(
            await _file_in_other_project({"project": wanted, "title": "x"}, OWN, request)
        )
        assert error in result["error"]
        assert all(method == "GET" for method, _, _ in calls)
