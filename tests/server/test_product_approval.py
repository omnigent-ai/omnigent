"""Which sign-off a ticket's open ask waits on, and who may give it in one click."""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.server.product_approval import sign_offs_for

OWNER, APPROVER, MEMBER = "owner@example.com", "approver@example.com", "member@example.com"
PROJECT = SimpleNamespace(user_id=OWNER, config={"product_approvers": [APPROVER]})
ASK = {"key": "needs-human"}


def ticket(*labels: dict, requested_by: str = OWNER) -> dict:
    return {"labels": list(labels), "requested_by": requested_by}


def test_risk_ok_is_offered_to_the_owner_only_while_the_ask_is_open() -> None:
    risky = ticket({"key": "risk", "value": "auth"}, ASK)
    assert sign_offs_for(PROJECT, risky, OWNER) == [{"key": "risk-ok", "value": None}]
    assert sign_offs_for(PROJECT, risky, APPROVER) == []
    assert sign_offs_for(PROJECT, risky, None) == []
    # A single-user server has no user ID; its one person is the owner.
    solo = SimpleNamespace(user_id=None, config={})
    assert sign_offs_for(solo, risky, None) == [{"key": "risk-ok", "value": None}]
    assert sign_offs_for(PROJECT, ticket({"key": "risk", "value": "auth"}), OWNER) == []
    given = ticket({"key": "risk", "value": "auth"}, {"key": "risk-ok"}, ASK)
    assert sign_offs_for(PROJECT, given, OWNER) == []


def test_product_ok_goes_to_approvers_and_owner_ok_binds_the_approved_sha() -> None:
    feature = ticket({"key": "kind", "value": "feature"}, ASK, requested_by=MEMBER)
    product_ok = [{"key": "product-ok", "value": None}]
    assert sign_offs_for(PROJECT, feature, APPROVER) == product_ok
    assert sign_offs_for(PROJECT, feature, OWNER) == product_ok
    assert sign_offs_for(PROJECT, feature, MEMBER) == []
    # The owner's own feature needs no product-ok.
    assert sign_offs_for(PROJECT, ticket({"key": "kind", "value": "feature"}, ASK), OWNER) == []
    sha = "a" * 40
    outside = ticket({"key": "approved", "value": sha}, ASK, requested_by="github:octocat")
    assert sign_offs_for(PROJECT, outside, OWNER) == [{"key": "owner-ok", "value": sha}]
    # Without an approved SHA there is nothing to bind owner-ok to yet.
    assert sign_offs_for(PROJECT, ticket(ASK, requested_by="github:octocat"), OWNER) == []
