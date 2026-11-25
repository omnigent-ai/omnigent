"""Which sign-off a ticket's open ask waits on, and who may give it in one click."""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.server.product_approval import (
    human_request_routing,
    owner_sign_off_pending,
    pending_sign_offs,
    risk_ok_givers,
    sign_offs_for,
)

OWNER, APPROVER, MEMBER = "owner@example.com", "approver@example.com", "member@example.com"
PROJECT = SimpleNamespace(user_id=OWNER, config={"product_approvers": [APPROVER]})
ASK = {"key": "needs-human"}


def ticket(*labels: dict, requested_by: str = OWNER) -> dict:
    return {"labels": list(labels), "requested_by": requested_by}


def test_risk_ok_is_offered_to_the_owner_and_approvers_even_on_their_own_request() -> None:
    risky = ticket({"key": "risk", "value": "auth"}, ASK, requested_by=MEMBER)
    risk_ok = [{"key": "risk-ok", "value": None}]
    assert sign_offs_for(PROJECT, risky, OWNER) == risk_ok
    assert sign_offs_for(PROJECT, risky, APPROVER) == risk_ok
    assert sign_offs_for(PROJECT, risky, MEMBER) == []
    assert sign_offs_for(PROJECT, risky, None) == []
    # An approver may sign off risk on a ticket they asked for, as the owner may.
    own = ticket({"key": "risk", "value": "auth"}, ASK, requested_by=APPROVER)
    assert sign_offs_for(PROJECT, own, APPROVER) == risk_ok
    assert sign_offs_for(PROJECT, ticket({"key": "risk", "value": "auth"}, ASK), OWNER) == risk_ok
    # A single-user server has no user ID; its one person is the owner.
    solo = SimpleNamespace(user_id=None, config={})
    assert sign_offs_for(solo, ticket({"key": "risk", "value": "a"}, ASK), None) == risk_ok
    assert sign_offs_for(PROJECT, ticket({"key": "risk", "value": "auth"}), OWNER) == []
    given = ticket({"key": "risk", "value": "auth"}, {"key": "risk-ok"}, ASK)
    assert sign_offs_for(PROJECT, given, OWNER) == []


def test_risk_ok_givers_routing_and_what_is_still_owed() -> None:
    # Every product approver may sign off risk, the requester included.
    assert risk_ok_givers(PROJECT) == {OWNER, APPROVER}
    risk = [{"key": "risk", "value": "auth"}, ASK]
    assert human_request_routing(PROJECT, risk)[1] == {OWNER, APPROVER}
    assert human_request_routing(PROJECT, [ASK])[1] == {OWNER}
    feature = [{"key": "kind", "value": "feature"}, {"key": "risk", "value": "a"}]
    assert pending_sign_offs(PROJECT, feature, MEMBER) == ["risk-ok", "product-ok"]
    assert pending_sign_offs(PROJECT, [*feature, {"key": "product-ok"}], MEMBER) == ["risk-ok"]
    assert pending_sign_offs(PROJECT, [{"key": "approved", "value": "x"}], "github:octocat") == [
        "owner-ok"
    ]
    assert not owner_sign_off_pending([{"key": "risk", "value": "a"}], MEMBER)


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


def test_an_ask_waits_on_product_ok_only_when_the_ticket_still_needs_it() -> None:
    feature = {"key": "kind", "value": "feature"}
    risk = {"key": "risk", "value": "auth"}
    # The owner's own feature never needs product-ok: a plain question goes to the owner.
    assert human_request_routing(PROJECT, [feature, ASK], OWNER) == ("owner", {OWNER})
    # With risk still owed it waits on risk-ok, from whoever may give it.
    assert human_request_routing(PROJECT, [feature, risk, ASK], OWNER) == (
        "risk-ok",
        {OWNER, APPROVER},
    )
    given = [feature, risk, {"key": "risk-ok"}, ASK]
    assert human_request_routing(PROJECT, given, OWNER) == ("owner", {OWNER})
    # A member's feature still waits on product-ok until it's given.
    assert human_request_routing(PROJECT, [feature, ASK], MEMBER) == (
        "product-ok",
        {OWNER, APPROVER},
    )
    approved = [feature, {"key": "product-ok"}, ASK]
    assert human_request_routing(PROJECT, approved, MEMBER) == ("owner", {OWNER})
