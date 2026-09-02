from maintainer_approval import approval_decision, latest_decisive_reviews

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


def decide(*, reviews, commits, head="new", head_repository=REPOSITORY):
    return approval_decision(
        repository=REPOSITORY,
        author="contributor",
        head_repository=head_repository,
        head_sha=head,
        maintainers=MAINTAINERS,
        trusted_successors=TRUSTED,
        reviews=reviews,
        commits=commits,
    )


def test_current_head_approval_passes():
    decision = decide(reviews=[review("APPROVED", "new")], commits=[commit("new")])
    assert decision.approved
    assert "Current head approved" in decision.reason


def test_approval_survives_trusted_same_repo_successor_commits():
    decision = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new")],
    )
    assert decision.approved
    assert "only trusted automation committed" in decision.reason


def test_approval_does_not_survive_untrusted_or_fork_updates():
    untrusted = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new", "contributor")],
    )
    assert not untrusted.approved
    assert "untrusted commit" in untrusted.reason

    fork = decide(
        reviews=[review("APPROVED", "old")],
        commits=[commit("old"), commit("new")],
        head_repository="contributor/omnigent",
    )
    assert not fork.approved
    assert "fork-head update" in fork.reason


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
        trusted_successors=TRUSTED,
        reviews=[],
        commits=[commit("new", "maintainer")],
    )
    assert decision.approved
