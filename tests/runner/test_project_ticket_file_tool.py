"""Tools that reach another project (file, message, board read) find it first."""

import json

import pytest
from pydantic import ValidationError

from omnigent.runner.project_tickets import (
    _file_in_other_project,
    _message_other_lead,
    _read_other_board,
)
from omnigent.tools.builtins.project_tickets import MessageOtherLead, ReadOtherBoard

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


async def test_board_read_maps_each_action_to_the_other_projects_read_path():
    base = f"/v1/projects/{OTHER}/board-reads"
    for args, path, params in (
        ({"stage_id": "open", "limit": 5}, f"{base}/tickets", {"stage_id": "open", "limit": 5}),
        ({"action": "get", "ticket_id": "t1"}, f"{base}/tickets/t1", None),
        (
            {"action": "comments", "ticket_id": "t1", "after": 3},
            f"{base}/tickets/t1/comments",
            None,
        ),
        ({"action": "stages"}, f"{base}/stages", None),
    ):
        seen: list[tuple[str, dict | None]] = []

        async def request(method, url, seen=seen, **kwargs):
            if url == "/v1/projects":
                return {"data": [{"id": OWN, "name": "Mine"}, {"id": OTHER, "name": "Hers"}]}
            seen.append((url, kwargs.get("params")))
            return {"object": "project.board_read"}

        body = ReadOtherBoard.model_validate({"project": "hers", **args}).model_dump(
            exclude_none=True
        )
        ticket_id = body.pop("ticket_id", None)
        await _read_other_board(body, ticket_id, OWN, request)
        assert seen[0][0] == path
        if params is not None:
            assert seen[0][1] == {**params}


def test_board_read_needs_a_ticket_for_get_and_comments():
    for action in ("get", "comments"):
        with pytest.raises(ValidationError):
            ReadOtherBoard(project="Hers", action=action)
