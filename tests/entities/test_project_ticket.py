import pytest
from pydantic import ValidationError

from omnigent.entities.project_ticket import (
    STATUS_CODES,
    CommentOnTicket,
    CreateTicket,
    TicketOutput,
    UpdateTicket,
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


def test_done_requires_summary_and_comments_have_no_notification_option():
    with pytest.raises(ValidationError):
        UpdateTicket(status="done")
    with pytest.raises(ValidationError):
        UpdateTicket(status="done", completion_summary="  ")
    assert (
        UpdateTicket(status="done", completion_summary=" Tested ").completion_summary == "Tested"
    )
    with pytest.raises(ValidationError):
        CommentOnTicket(text="Progress", notify_orchestrator=False)


def test_lifecycle_preserves_status_codes_and_requires_review_evidence():
    assert {name: STATUS_CODES[name] for name in ("open", "in_progress", "done")} == {
        "open": 0,
        "in_progress": 1,
        "done": 2,
    }
    assert len(set(STATUS_CODES.values())) == 5
    assert UpdateTicket(status="planning", plan="Approach and done when").plan
    for summary in (None, "", "  "):
        with pytest.raises(ValidationError, match="Review"):
            UpdateTicket(status="review", review_summary=summary)
    assert (
        UpdateTicket(status="review", review_summary="Verify the attached demo").status == "review"
    )
    assert "lead_session_id" not in UpdateTicket(title="Keep lead").model_fields_set
    assert "lead_session_id" in UpdateTicket(lead_session_id=None).model_fields_set
    with pytest.raises(ValidationError):
        UpdateTicket(comment=" ")


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
