"""A pull request's merge queue, against the fake ``gh`` and GraphQL endpoint of the review writes.

Adding refuses a moved head before any mutation is sent, and each change is refused while
the queue can't take it: not turned on, already queued, or not queued.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runner import pr_actions
from omnigent.runner.pull_request_errors import (
    PULL_REQUEST_CHANGED_MESSAGE,
    InvalidPullRequestInput,
    PullRequestChangedError,
    UnknownOutcomeError,
)
from tests.runner.test_github_review_writes import HEAD, MOVED, PR_URL, FakeGitHub

SESSION = "session_one"


class QueueGitHub(FakeGitHub):
    """The review writes' fake GitHub, with a merge queue on the pull request's base branch."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        self.queue: dict[str, Any] = {
            "isMergeQueueEnabled": True,
            "isInMergeQueue": False,
            "mergeQueueEntry": None,
        }

    def _node(self, number: int) -> dict[str, Any]:
        return {**super()._node(number), **self.queue}

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        refused = any(name in query for name in self.refused)
        if "enqueuePullRequest(" in query and not refused:
            entry = {"position": 2, "state": "QUEUED"}
            return {"data": {"enqueuePullRequest": {"mergeQueueEntry": entry}}}
        if "dequeuePullRequest(" in query and not refused:
            return {"data": {"dequeuePullRequest": {"mergeQueueEntry": {"id": "MQE_1"}}}}
        return super()._graphql(query, variables)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> QueueGitHub:
    return QueueGitHub(monkeypatch)


def _enqueue(expected_head_oid: object = HEAD) -> dict[str, Any]:
    return pr_actions.enqueue_pull_request(
        "/workspace", session_id=SESSION, pr_url=PR_URL, expected_head_oid=expected_head_oid
    )


def _dequeue() -> dict[str, Any]:
    return pr_actions.dequeue_pull_request("/workspace", session_id=SESSION, pr_url=PR_URL)


def test_a_pull_request_outside_the_queue_reports_no_place_in_it(fake: QueueGitHub) -> None:
    answer = pr_actions.get_merge_queue("/workspace", session_id=SESSION, pr_url=PR_URL)

    assert answer == {
        "object": "session.github.merge_queue",
        "pr_url": PR_URL,
        "enabled": True,
        "queued": False,
        "position": None,
        "state": None,
        "head_sha": HEAD,
    }


def test_a_queued_pull_request_reports_its_place_and_state(fake: QueueGitHub) -> None:
    fake.queue.update(
        isInMergeQueue=True, mergeQueueEntry={"position": 3, "state": "AWAITING_CHECKS"}
    )

    answer = pr_actions.get_merge_queue("/workspace", session_id=SESSION, pr_url=PR_URL)

    assert (answer["queued"], answer["position"], answer["state"]) == (
        True,
        3,
        "AWAITING_CHECKS",
    )


def test_enqueue_refuses_a_moved_head_before_any_mutation(fake: QueueGitHub) -> None:
    fake.heads[7] = MOVED

    with pytest.raises(PullRequestChangedError) as refused:
        _enqueue()

    assert str(refused.value) == PULL_REQUEST_CHANGED_MESSAGE
    assert fake.mutations("enqueuePullRequest") == []


def test_enqueue_adds_the_head_the_user_saw_and_reports_its_place(fake: QueueGitHub) -> None:
    answer = _enqueue()

    assert fake.mutations("enqueuePullRequest") == [
        {"input": {"pullRequestId": "PR_7", "expectedHeadOid": HEAD}}
    ]
    assert (answer["queued"], answer["position"], answer["state"]) == (True, 2, "QUEUED")


@pytest.mark.parametrize("payload", [{"mergeQueueEntry": None}, {}], ids=["null", "missing"])
def test_an_enqueue_github_answered_without_its_entry_is_an_unknown_outcome(
    fake: QueueGitHub, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    answer = fake._graphql

    def without_entry(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "enqueuePullRequest(" in query:
            return {"data": {"enqueuePullRequest": payload}}
        return answer(query, variables)

    monkeypatch.setattr(fake, "_graphql", without_entry)

    with pytest.raises(UnknownOutcomeError):
        _enqueue()


def test_enqueue_reports_a_head_that_moved_while_github_refused_it(fake: QueueGitHub) -> None:
    fake.refuse_mutation("enqueuePullRequest", "Head sha didn't match", head_becomes=MOVED)

    with pytest.raises(PullRequestChangedError):
        _enqueue()


@pytest.mark.parametrize(
    ("queue", "message"),
    [
        (
            {"isMergeQueueEnabled": False},
            "The merge queue isn't turned on for this pull request's branch.",
        ),
        ({"isInMergeQueue": True}, "This pull request is already in the merge queue."),
    ],
    ids=["queue-off", "already-queued"],
)
def test_enqueue_is_refused_when_the_queue_cant_take_it(
    fake: QueueGitHub, queue: dict[str, Any], message: str
) -> None:
    fake.queue.update(queue)

    with pytest.raises(ValueError) as refused:
        _enqueue()

    assert str(refused.value) == message

    assert fake.mutations("enqueuePullRequest") == []


def test_enqueue_refuses_a_head_that_isnt_a_commit_before_reading_github(
    fake: QueueGitHub,
) -> None:
    with pytest.raises(InvalidPullRequestInput):
        _enqueue("not-a-commit")

    assert fake.json_calls == []


def test_dequeue_names_the_pull_request_and_reports_it_left(fake: QueueGitHub) -> None:
    fake.queue.update(isInMergeQueue=True, mergeQueueEntry={"position": 1, "state": "QUEUED"})

    answer = _dequeue()

    assert fake.mutations("dequeuePullRequest") == [{"input": {"id": "PR_7"}}]
    assert (answer["queued"], answer["position"], answer["state"]) == (False, None, None)


def test_dequeue_is_refused_for_a_pull_request_outside_the_queue(fake: QueueGitHub) -> None:
    with pytest.raises(ValueError) as refused:
        _dequeue()

    assert str(refused.value) == "This pull request isn't in the merge queue."

    assert fake.mutations("dequeuePullRequest") == []


def test_a_dequeue_github_answered_with_errors_is_an_unknown_outcome(fake: QueueGitHub) -> None:
    fake.queue["isInMergeQueue"] = True
    fake.errors_with_payload.add("dequeuePullRequest")

    with pytest.raises(UnknownOutcomeError):
        _dequeue()
