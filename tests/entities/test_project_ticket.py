import pytest
from pydantic import ValidationError

from omnigent.entities.project_ticket import (
    LEGACY_STATUS_CODES,
    CommentOnTicket,
    CreateTicket,
    LinkTicketSession,
    TicketOutput,
    TicketStage,
    TicketStages,
    UpdateTicket,
    UpdateTicketStages,
    default_ticket_stages,
)


def test_title_only_and_key_value_tags():
    assert CreateTicket(title="  Ship it  ").model_dump() == {
        "title": "Ship it",
        "description": "",
        "tags": [],
    }
    ticket = CreateTicket(title="Review", tags=[{"key": "needs-human", "value": "approval"}])
    assert ticket.tags[0].key == "needs-human"
    with pytest.raises(ValidationError):
        CreateTicket(title=" ")
    with pytest.raises(ValidationError):
        CreateTicket(title="Review", tags=[{"key": "team"}, {"key": " team "}])


def test_stage_ids_are_dynamic_and_comments_have_no_notification_option():
    assert UpdateTicket(stage_id="investigating").stage_id == "investigating"
    assert UpdateTicket(stage_id="done").completion_summary is None
    with pytest.raises(ValidationError):
        UpdateTicket(stage_id="resolved", completion_summary="  ")
    assert (
        UpdateTicket(stage_id="resolved", completion_summary=" Tested ").completion_summary
        == "Tested"
    )
    with pytest.raises(ValidationError):
        CommentOnTicket(text="Progress", notify_orchestrator=False)


def test_legacy_codes_and_optional_plan_review_content_are_preserved():
    assert {name: LEGACY_STATUS_CODES[name] for name in ("open", "in_progress", "done")} == {
        "open": 0,
        "in_progress": 1,
        "done": 2,
    }
    assert len(set(LEGACY_STATUS_CODES.values())) == 5
    assert UpdateTicket(stage_id="investigating", plan="Approach and done when").plan
    for summary in (None, "", "  "):
        assert UpdateTicket(stage_id="review", review_summary=summary).stage_id == "review"
    assert (
        UpdateTicket(stage_id="verifying", review_summary="Verify the attached demo").stage_id
        == "verifying"
    )
    assert "lead_session_id" not in UpdateTicket(title="Keep lead").model_fields_set
    assert "lead_session_id" in UpdateTicket(lead_session_id=None).model_fields_set
    with pytest.raises(ValidationError):
        UpdateTicket(comment=" ")


def test_stage_definitions_have_stable_defaults_and_trimmed_content():
    defaults = default_ticket_stages()
    assert [stage.id for stage in defaults.stages] == [
        "open",
        "planning",
        "in_progress",
        "review",
        "done",
    ]
    assert defaults.initial_stage_id == "open"
    assert [stage.id for stage in defaults.stages if stage.is_complete] == ["done"]
    renamed = TicketStage(id="open", name=" New ", instructions=" Investigate first ")
    assert renamed.name == "New" and renamed.instructions == "Investigate first"
    assert renamed.id == "open"
    defaults.stages[0].name = "Changed"
    assert default_ticket_stages().stages[0].name == "Open"


@pytest.mark.parametrize(
    "changes",
    [
        {"stages": []},
        {"stages": [{"id": "open", "name": "A"}, {"id": "open", "name": "B"}]},
        {"stages": [{"id": "open", "name": " New "}, {"id": "other", "name": "new"}]},
        {"stages": [{"id": "open", "name": " "}]},
        {"stages": [{"id": "open", "name": "x" * 61}]},
        {"stages": [{"id": "open", "name": "New", "instructions": "x" * 501}]},
        {"stages": [{"id": "../other", "name": "New"}]},
        {"stages": [{"id": "x" * 65, "name": "New"}]},
        {"stages": [{"id": "open", "name": "New", "is_complete": True}]},
        {"initial_stage_id": "missing"},
        {"stages": [{"id": f"stage-{index}", "name": str(index)} for index in range(21)]},
    ],
)
def test_stage_configuration_validation(changes):
    with pytest.raises(ValidationError):
        TicketStages.model_validate({**default_ticket_stages().model_dump(), **changes})


@pytest.mark.parametrize("moves", [{"open": "done"}, {"removed": "missing"}])
def test_stage_removal_targets_must_survive(moves):
    with pytest.raises(ValidationError):
        UpdateTicketStages(**default_ticket_stages().model_dump(), moves=moves)


def test_session_involvement_is_optional_bounded_and_only_changes_on_attach():
    assert LinkTicketSession(session_id="worker").involvement is None
    assert LinkTicketSession(session_id="worker", involvement=None).involvement is None
    assert (
        LinkTicketSession(session_id="worker", involvement=" Plan the work ").involvement
        == "Plan the work"
    )
    assert LinkTicketSession(session_id="worker", involvement="  ").involvement == ""
    assert LinkTicketSession(session_id="worker", action="remove").action == "remove"
    with pytest.raises(ValidationError):
        LinkTicketSession(session_id="worker", involvement="x" * 301)
    with pytest.raises(ValidationError, match="only valid"):
        LinkTicketSession(session_id="worker", action="remove", involvement="")


@pytest.mark.parametrize("suffix", ["", "/files", "?view=1#discussion"])
def test_pr_outputs_are_canonical(suffix):
    output = TicketOutput(
        type="pull_request", url="https://GIT.example.com/team/repo/pull/42" + suffix
    )
    assert output.url == "https://git.example.com/team/repo/pull/42"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:pass@example.com/pull/1",
        "https://example.com/issues/1",
        "https://example.com/pull/0",
        "https://example.com/pull/1\n",
    ],
)
def test_invalid_outputs_are_rejected(url):
    with pytest.raises(ValidationError):
        TicketOutput(type="pull_request", url=url)
