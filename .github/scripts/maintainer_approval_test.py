from maintainer_approval import (
    approval_decision,
    latest_decisive_reviews,
    pull_request_target_pushers,
)

REPOSITORY = "omnigent-ai/omnigent"
MAINTAINERS = {"maintainer"}
TRUSTED = {"omni-resolve-agent[bot]"}


def review(state: str, commit_id: str, *, submitted: str = "2026-09-01T00:00:00Z"):
    return {
        "id": 1,
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted,
        "user": {"login": "maintainer"},
    }


def commit(sha: str, login: str = "omni-resolve-agent[bot]"):
    return {
        "sha": sha,
        "author": {"login": login},
        "committer": {"login": login},
    }


def dismissal_event(
    *,
    review_id=1,
    actor="omni-resolve-agent[bot]",
    commit_id="new",
    dismissal_message=None,
):
    return {
        "event": "review_dismissed",
        "actor": {"login": actor},
        "dismissed_review": {
            "review_id": review_id,
            "state": "approved",
            "dismissal_commit_id": commit_id,
            "dismissal_message": dismissal_message,
        },
    }


def decide(
    *,
    reviews,
    commits,
    timeline=None,
    head="new",
    head_repository=REPOSITORY,
    author="contributor",
    pushers=None,
):
    return approval_decision(
        repository=REPOSITORY,
        author=author,
        head_repository=head_repository,
        head_sha=head,
        maintainers=MAINTAINERS,
        trusted_authors=TRUSTED,
        trusted_successors=TRUSTED,
        reviews=reviews,
        commits=commits,
        pushers=pushers or (lambda _sha: set(TRUSTED)),
        timeline=timeline or [],
    )


def test_current_head_approval_passes():
    decision = decide(reviews=[review("APPROVED", "new")], commits=[commit("new")])
    assert decision.approved
    assert "Current head approved" in decision.reason


def test_trusted_automation_author_passes_without_review():
    decision = decide(
        author="omni-resolve-agent[bot]",
        reviews=[],
        commits=[commit("new")],
    )
    assert decision.approved
    assert "trusted automation" in decision.reason


def test_approval_survives_trusted_same_repo_successor_commits():
    decision = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new")],
    )
    assert decision.approved
    assert "only trusted automation pushed and committed" in decision.reason


def test_auto_dismissed_approval_survives_the_trusted_push_that_dismissed_it():
    decision = decide(
        reviews=[review("DISMISSED", "old")],
        commits=[commit("old"), commit("new")],
        timeline=[dismissal_event()],
    )
    assert decision.approved
    assert "only trusted automation pushed and committed" in decision.reason


def test_fork_head_requires_a_fresh_approval():
    decision = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new")],
        head_repository="contributor/omnigent",
    )
    assert not decision.approved
    assert "fork head" in decision.reason


def test_successor_pushed_by_untrusted_actor_is_rejected_despite_bot_commit_identity():
    decision = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new")],
        pushers=lambda sha: {"contributor"} if sha == "new" else set(TRUSTED),
    )
    assert not decision.approved
    assert "untrusted commit" in decision.reason


def test_intermediate_untrusted_push_is_rejected_even_when_automation_pushed_last():
    decision = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("mid"), commit("new")],
        pushers=lambda sha: {"contributor"} if sha == "mid" else set(TRUSTED),
    )
    assert not decision.approved
    assert "mid" in decision.reason


def test_head_without_a_trusted_push_record_is_rejected():
    decision = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new")],
        pushers=lambda _sha: set(),
    )
    assert not decision.approved
    assert "no push recorded from trusted automation" in decision.reason


def test_dismissal_by_untrusted_actor_is_rejected_despite_bot_commit_identity():
    decision = decide(
        reviews=[review("DISMISSED", "old")],
        commits=[commit("old"), commit("new")],
        timeline=[dismissal_event(actor="contributor")],
    )
    assert not decision.approved
    assert "not auto-dismissed by trusted automation" in decision.reason


def test_pushers_come_from_every_page_of_pull_request_target_run_actors():
    calls = []
    pages = {
        1: [{"actor": {"login": "omni-resolve-agent[bot]"}}] * 99 + [{"actor": None}],
        2: [{"actor": {"login": "contributor"}}],
    }

    def request(arguments):
        calls.append(arguments[1])
        return {"workflow_runs": pages[int(arguments[1].rsplit("page=", 1)[1])]}

    pushers = pull_request_target_pushers(REPOSITORY, request)
    assert pushers("abc") == {"omni-resolve-agent[bot]", "contributor"}
    assert pushers("abc") == {"omni-resolve-agent[bot]", "contributor"}
    endpoint = (
        f"repos/{REPOSITORY}/actions/runs?head_sha=abc&event=pull_request_target&per_page=100"
    )
    assert calls == [f"{endpoint}&page=1", f"{endpoint}&page=2"]


def test_dismissed_approval_requires_a_trusted_matching_dismissal_event():
    manual = decide(
        reviews=[review("DISMISSED", "old")],
        commits=[commit("old"), commit("new")],
        timeline=[dismissal_event(actor="maintainer", dismissal_message="withdrawn")],
    )
    wrong_commit = decide(
        reviews=[review("DISMISSED", "old")],
        commits=[commit("old"), commit("new")],
        timeline=[dismissal_event(commit_id="other")],
    )
    assert not manual.approved
    assert not wrong_commit.approved


def test_approval_does_not_survive_untrusted_updates():
    untrusted = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new", "contributor")],
    )
    assert not untrusted.approved
    assert "untrusted commit" in untrusted.reason

    untrusted_fork = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new", "contributor")],
        head_repository="contributor/omnigent",
    )
    assert not untrusted_fork.approved
    assert "fork head" in untrusted_fork.reason


def test_approval_does_not_survive_rewritten_history():
    decision = decide(
        reviews=[review("APPROVED", "removed")],
        commits=[commit("new")],
    )
    assert not decision.approved
    assert "no longer in PR history" in decision.reason


def test_later_changes_requested_supersedes_approval():
    reviews = [
        review("APPROVED", "old"),
        review("CHANGES_REQUESTED", "new", submitted="2026-09-02T00:00:00Z"),
    ]
    assert latest_decisive_reviews(reviews)["maintainer"]["state"] == "CHANGES_REQUESTED"
    assert not decide(reviews=reviews, commits=[commit("old"), commit("new")]).approved


def test_maintainer_authored_pr_still_passes():
    decision = approval_decision(
        repository=REPOSITORY,
        author="maintainer",
        head_repository=REPOSITORY,
        head_sha="new",
        maintainers=MAINTAINERS,
        trusted_authors=TRUSTED,
        trusted_successors=TRUSTED,
        reviews=[],
        commits=[commit("new", "maintainer")],
        pushers=lambda _sha: set(),
    )
    assert decision.approved
