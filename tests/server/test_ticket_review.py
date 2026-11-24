"""Review-step helpers: approval gating, superseding SHAs and the escalation reminder."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.persona_mentions import Mention
from omnigent.server.ticket_review import (
    check_approval_change,
    find_reviewer,
    is_reviewer_session,
    superseding_sha,
    to_reviewer_message,
)

REVIEWER = Mention("rev", "Reviewer")
SHA_A, SHA_B = "a" * 40, "b" * 40


def test_only_the_reviewer_or_owner_sets_a_full_sha_approval() -> None:
    check_approval_change(None, SHA_A, author_session_id="s", author_is_reviewer=True)
    check_approval_change(None, SHA_A, author_session_id=None, author_is_reviewer=False)
    check_approval_change(SHA_A, None, author_session_id="worker", author_is_reviewer=False)
    with pytest.raises(OmnigentError, match="Only the Reviewer"):
        check_approval_change(None, SHA_A, author_session_id="worker", author_is_reviewer=False)
    with pytest.raises(OmnigentError, match="40-character"):
        check_approval_change(None, "A" * 40, author_session_id=None, author_is_reviewer=False)


def test_reviewer_is_found_by_name_and_session_label() -> None:
    assert find_reviewer([Mention("pm", "PM"), Mention("rev", "reviewer")]).id == "rev"
    assert find_reviewer([Mention("pm", "PM")]) is None
    labelled = SimpleNamespace(labels={"omnigent.project_identity_id": "rev"})
    assert is_reviewer_session(labelled, REVIEWER)
    assert not is_reviewer_session(SimpleNamespace(labels={}), REVIEWER)


def test_a_different_sha_supersedes_the_approval_and_round_three_escalates() -> None:
    assert superseding_sha(f"rebased to {SHA_B}", SHA_A) == SHA_B
    assert superseding_sha(f"still {SHA_A}", SHA_A) is None
    # Restating the approved head with its base doesn't clear the approval.
    assert superseding_sha(f"still at {SHA_A}, on base {'c' * 40}", SHA_A) is None
    assert superseding_sha(f"new {SHA_B}", None) is None
    ticket = {"id": "t", "title": "T"}
    assert "escalate" not in to_reviewer_message(ticket, "fixed", "w", rounds=2)
    assert "round 3 without approval" in to_reviewer_message(ticket, "fixed", "w", rounds=3)


def test_only_a_ticket_before_landing_can_have_its_approval_superseded() -> None:
    from omnigent.server.ticket_review import approval_can_be_superseded

    stages = ["open", "in_progress", "code_review", "implemented", "done", "merged"]
    assert [approval_can_be_superseded(stage, stages) for stage in stages] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]
    assert not approval_can_be_superseded(None, stages)
    assert not approval_can_be_superseded("gone", stages)
    # Without a Review stage, the last two stages are landed and complete.
    plain = ["open", "in_progress", "review", "done"]
    assert [approval_can_be_superseded(stage, plain) for stage in plain] == [
        True,
        True,
        False,
        False,
    ]
