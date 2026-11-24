from omnigent.server.ticket_responder import ROUTER, assignee_name, responder_order


def _order(**overrides):
    inputs = {
        "linked": ["old-worker", "reviewer", "worker"],
        "routers": {"lead"},
        "reviewer": "reviewer",
        "assignee": None,
        "asker": None,
        "commenters": [],
    }
    return responder_order(**{**inputs, **overrides})


def test_defaults_to_the_newest_worker_then_reviewer_then_router() -> None:
    assert _order() == ["worker", "old-worker", "reviewer", ROUTER]


def test_the_most_recent_commenter_is_the_active_worker() -> None:
    assert _order(commenters=["old-worker", "worker"])[0] == "old-worker"


def test_answers_go_to_the_asker_or_latest_commenter_even_the_router() -> None:
    assert _order(asker="reviewer", commenters=["worker"])[:2] == ["reviewer", "worker"]
    assert _order(commenters=["lead", "worker"])[0] is ROUTER
    # A cc'd persona that isn't linked still gets the reply to its own comment.
    assert _order(commenters=["pm"])[0] == "pm"


def test_assignee_outranks_the_worker_heuristic() -> None:
    assert _order(assignee="old-worker", commenters=[])[0] == "old-worker"


def test_no_worker_falls_back_to_the_router() -> None:
    assert _order(linked=["lead"]) == [ROUTER]
    # An unlinked Reviewer isn't woken by default.
    assert _order(linked=[]) == [ROUTER]


def test_assignee_name_reads_the_label() -> None:
    assert (
        assignee_name([{"key": "kind", "value": "ux"}, {"key": "assignee", "value": " PM "}])
        == "PM"
    )
    assert assignee_name([{"key": "assignee", "value": ""}]) is None
    assert assignee_name(None) is None
