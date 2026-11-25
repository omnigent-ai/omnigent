"""sys_project_ticket_file and sys_project_lead_message find the other project first."""

import json

import pytest
from pydantic import ValidationError

from omnigent.runner.project_tickets import _file_in_other_project, _message_other_lead
from omnigent.tools.builtins.project_tickets import MessageOtherLead

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


async def test_lead_message_goes_to_the_named_project_and_a_reply_to_its_thread():
    request, calls = _requester([{"id": OWN, "name": "Mine"}, {"id": OTHER, "name": "Hers"}])
    await _message_other_lead({"project": "Hers", "message": "How?"}, OWN, request)
    assert calls[-1] == ("POST", f"/v1/projects/{OTHER}/lead-messages", {"message": "How?"})

    request, calls = _requester([])
    await _message_other_lead({"reply_to": "d" * 32, "message": "Like this."}, OWN, request)
    assert calls == [
        (
            "POST",
            f"/v1/projects/{OWN}/lead-messages/{'d' * 32}/reply",
            {"message": "Like this."},
        )
    ]


async def test_a_malformed_reply_to_is_an_error_not_a_crash():
    request, calls = _requester([])
    result = json.loads(
        await _message_other_lead({"reply_to": "not-an-id", "message": "x"}, OWN, request)
    )
    assert "reply_to" in result["error"] and calls == []


def test_lead_message_takes_exactly_one_of_project_or_reply_to():
    for args in ({"message": "x"}, {"project": "Hers", "reply_to": "d" * 32, "message": "x"}):
        with pytest.raises(ValidationError):
            MessageOtherLead.model_validate(args)
