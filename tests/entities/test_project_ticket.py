import pytest
from pydantic import ValidationError

from omnigent.entities.project_ticket import (
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
