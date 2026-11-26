"""The viewer's pending review, read with its drafts, against a fake GitHub GraphQL API.

The read is bounded: drafts and threads are paged up to a limit, hunks and the whole answer
are capped, and whatever any bound cuts off makes the answer incomplete. Answers are kept
per review until something says the review moved, or a pending-review write is made.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.runner import github_pending_reviews as pending_reviews
from omnigent.runner import github_resource, github_reviews
from omnigent.runner.pull_request_errors import PullRequestError
from omnigent.runner.session_prs import PullRequestRef

PR = PullRequestRef.from_url("https://github.com/example-org/example-repo/pull/7")
HEAD, BASE, MOVED = "a" * 40, "b" * 40, "c" * 40
REVIEW = "PRR_kwDOAAAA"


class FakeGitHub:
    """One PR with the viewer's pending review: its drafts, and the threads holding them."""

    def __init__(self, drafts: int = 0, *, per_thread: int = 1) -> None:
        self.heads = {"headRefOid": HEAD, "baseRefOid": BASE}
        # The heads the thread pages answer at; a push mid-read moves them.
        self.thread_heads = dict(self.heads)
        self.review: dict[str, Any] | None = {
            "id": REVIEW,
            "body": "Summary",
            "viewerDidAuthor": True,
            "commit": {"oid": HEAD},
        }
        self.drafts = [self.draft(index) for index in range(drafts)]
        ids = [draft["id"] for draft in self.drafts]
        self.threads = [
            self.thread(f"PRRT_{start}", ids[start : start + per_thread])
            for start in range(0, len(ids), per_thread)
        ]
        # A count GitHub reports apart from the list, as when drafts come and go mid-read.
        self.count_override: int | None = None
        self.node_reads = 0
        self.graphql_reads: list[str] = []

    @staticmethod
    def draft(index: int, *, body: str = "Draft", reply_to: str | None = None) -> dict[str, Any]:
        return {
            "id": f"PRRC_{index}",
            "body": f"{body} {index}",
            "path": "src/parser.py",
            "line": 2,
            "state": "PENDING",
            "replyTo": {"id": reply_to} if reply_to else None,
        }

    def thread(self, thread_id: str, draft_ids: list[str], hunk: str = "@@ -1 +1 @@") -> dict:
        return {
            "id": thread_id,
            "path": "src/parser.py",
            "diffSide": "RIGHT",
            "line": 2,
            "subjectType": "LINE",
            "viewerCanReply": True,
            "comments": {
                "nodes": [
                    {
                        "id": draft_id,
                        "author": {"login": "viewer"},
                        "body": "Draft",
                        "state": "PENDING",
                        "diffHunk": hunk,
                        "viewerCanUpdate": True,
                        "viewerCanDelete": True,
                        "viewerDidAuthor": True,
                        "pullRequestReview": {"id": REVIEW},
                    }
                    for draft_id in draft_ids
                ]
            },
        }

    def count(self) -> int:
        return len(self.drafts) if self.count_override is None else self.count_override

    def pull_request_node(self, _root: str, reference: PullRequestRef, selection: str) -> dict:
        assert reference == PR
        assert selection == pending_reviews.OWN_PENDING_REVIEW
        self.node_reads += 1
        review = {**self.review, "comments": {"totalCount": self.count()}} if self.review else None
        return {"id": "PR_1", **self.heads, "reviews": {"nodes": [review] if review else []}}

    def graphql(self, _root: str, _reference: PullRequestRef, query: str, **variables: Any):
        start = int(variables.get("after") or 0)
        end = start + variables["first"]
        if "PullRequestReview" in query:
            self.graphql_reads.append("comments")
            assert variables["id"] == REVIEW
            nodes = self.drafts[start:end]
            connection = {
                "totalCount": self.count(),
                "nodes": nodes,
                "pageInfo": {"hasNextPage": end < len(self.drafts), "endCursor": str(end)},
            }
            review = {"id": REVIEW, "state": "PENDING", "viewerDidAuthor": True}
            return {"data": {"node": {**review, "comments": connection}}}
        self.graphql_reads.append("threads")
        assert variables["drafts"] == pending_reviews.DRAFT_COMMENTS_PER_THREAD
        connection = {
            "nodes": self.threads[start:end],
            "pageInfo": {"hasNextPage": end < len(self.threads), "endCursor": str(end)},
        }
        pull_request = {**self.thread_heads, "reviewThreads": connection}
        return {"data": {"repository": {"pullRequest": pull_request}}}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    github = FakeGitHub()
    monkeypatch.setattr(pending_reviews, "pull_request_node", github.pull_request_node)
    monkeypatch.setattr(pending_reviews, "_graphql", github.graphql)
    return github


@pytest.fixture(autouse=True)
def _no_kept_answers() -> None:
    pending_reviews._cache.clear()
    pending_reviews._generations.clear()


def _use(fake: FakeGitHub, other: FakeGitHub) -> FakeGitHub:
    """Point ``fake``'s patched reads at ``other``'s PR."""
    fake.__dict__.update(other.__dict__)
    return fake


def _read() -> dict[str, Any]:
    return pending_reviews.get_pending_review("/workspace", PR)


def _listed(answer: dict[str, Any]) -> list[str]:
    placed = [d["id"] for thread in answer["threads"] for d in thread["comments"]["data"]]
    return placed + [draft["id"] for draft in answer["unplaced"]]


def test_3000_drafts_read_whole_across_30_pages_of_each(fake: FakeGitHub) -> None:
    _use(fake, FakeGitHub(3000))

    answer = _read()

    assert answer["complete"] is True
    assert answer["review"]["comment_count"] == answer["review"]["read_count"] == 3000
    assert sorted(_listed(answer)) == sorted(f"PRRC_{index}" for index in range(3000))
    assert answer["unplaced"] == []
    assert fake.graphql_reads.count("comments") == 30
    assert fake.graphql_reads.count("threads") == 30


def test_drafts_past_the_page_limit_make_the_answer_incomplete(fake: FakeGitHub) -> None:
    _use(fake, FakeGitHub(3001))

    answer = _read()

    assert answer["complete"] is False
    assert answer["review"]["comment_count"] == 3001
    assert answer["review"]["read_count"] == len(_listed(answer)) == 3000
    assert fake.graphql_reads.count("comments") == pending_reviews.MAX_DRAFT_PAGES


def test_threads_past_the_page_limit_leave_their_drafts_unplaced(fake: FakeGitHub) -> None:
    github = _use(fake, FakeGitHub(3000))
    # Another reviewer's threads come first, so the drafts' threads lie past the limit.
    others = [github.thread(f"PRRT_other_{index}", []) for index in range(3000)]
    github.threads = others + github.threads

    answer = _read()

    assert answer["complete"] is False
    assert answer["threads"] == []
    assert len(answer["unplaced"]) == answer["review"]["read_count"] == 3000


def test_a_long_hunk_is_cut_at_a_line_and_flagged(fake: FakeGitHub) -> None:
    github = _use(fake, FakeGitHub(2))
    long_hunk = "@@ -1,600 +1,600 @@\n" + "".join(f"+line {index:04d} é\n" for index in range(900))
    github.threads = [
        github.thread("PRRT_long", ["PRRC_0"], hunk=long_hunk),
        github.thread("PRRT_short", ["PRRC_1"]),
    ]

    answer = _read()

    long_thread, short_thread = answer["threads"]
    assert long_thread["diff_hunk_truncated"] is True
    assert len(long_thread["diff_hunk"].encode()) <= pending_reviews.MAX_DIFF_HUNK_BYTES
    assert long_hunk.startswith(long_thread["diff_hunk"] + "\n")
    assert short_thread["diff_hunk_truncated"] is False
    assert short_thread["diff_hunk"] == "@@ -1 +1 @@"
    # A cut hunk says nothing about the drafts: the list is still whole.
    assert answer["complete"] is True


def test_an_answer_past_its_cap_drops_its_last_drafts(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _use(fake, FakeGitHub(40))
    for thread in github.threads:
        for comment in thread["comments"]["nodes"]:
            comment["body"] = "x" * 2000
    monkeypatch.setattr(pending_reviews, "MAX_ANSWER_BYTES", 30_000)

    answer = _read()

    listed = _listed(answer)
    assert answer["complete"] is False
    assert 0 < len(listed) < 40
    assert answer["review"]["read_count"] == len(listed)
    assert answer["review"]["comment_count"] == 40
    assert listed == [f"PRRC_{index}" for index in range(len(listed))]


def test_a_large_answer_is_cut_to_its_cap_in_one_pass(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3,000 long drafts, about 7 MB: each thread and draft is measured at most once."""
    github = _use(fake, FakeGitHub(3000))
    for thread in github.threads:
        for comment in thread["comments"]["nodes"]:
            comment["body"] = "é" * 1200
    measured = 0
    measure = pending_reviews._json_bytes

    def counted(value: Any) -> int:
        nonlocal measured
        measured += 1
        return measure(value)

    monkeypatch.setattr(pending_reviews, "_json_bytes", counted)
    answer = _read()

    # The frame, then one measure per thread shell and per draft.
    assert measured <= 1 + len(github.threads) + 3000
    size = len(json.dumps(answer).encode("utf-8"))
    # Short of the cap by less than one more thread of about 7 KB.
    assert pending_reviews.MAX_ANSWER_BYTES - 16_384 < size <= pending_reviews.MAX_ANSWER_BYTES
    listed = _listed(answer)
    assert answer["complete"] is False
    assert answer["review"]["read_count"] == len(listed) < 3000
    assert listed == [f"PRRC_{index}" for index in range(len(listed))]


def test_replies_and_drafts_outside_any_thread(fake: FakeGitHub) -> None:
    github = _use(fake, FakeGitHub())
    github.drafts = [
        FakeGitHub.draft(0),
        FakeGitHub.draft(1, reply_to="PRRC_submitted"),
        FakeGitHub.draft(2),
    ]
    github.threads = [github.thread("PRRT_a", ["PRRC_0", "PRRC_1"])]

    answer = _read()

    [thread] = answer["threads"]
    assert [(d["id"], d["reply_to_id"]) for d in thread["comments"]["data"]] == [
        ("PRRC_0", None),
        ("PRRC_1", "PRRC_submitted"),
    ]
    assert [draft["id"] for draft in answer["unplaced"]] == ["PRRC_2"]
    assert answer["complete"] is True


def test_a_push_while_reading_leaves_every_draft_unplaced(fake: FakeGitHub) -> None:
    github = _use(fake, FakeGitHub(3))
    github.thread_heads["headRefOid"] = MOVED

    answer = _read()

    assert answer["threads"] == []
    assert len(answer["unplaced"]) == 3
    assert answer["complete"] is False


def test_drafts_that_come_or_go_between_pages_are_incomplete(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _use(fake, FakeGitHub(150))
    reads = 0
    original = github.graphql

    def count_moves(*args: Any, **variables: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 2:
            github.count_override = 151
        return original(*args, **variables)

    monkeypatch.setattr(pending_reviews, "_graphql", count_moves)

    answer = _read()

    assert answer["complete"] is False


@pytest.mark.parametrize("review", [None, {"id": REVIEW, "viewerDidAuthor": False}])
def test_no_review_of_the_viewers_reads_as_none(fake: FakeGitHub, review: Any) -> None:
    fake.review = review

    answer = _read()

    assert answer["review"] is None
    assert answer["complete"] is True
    assert fake.graphql_reads == []
    with pytest.raises(PullRequestError, match="no pending review"):
        pending_reviews.own_pending_review("/workspace", PR)


@pytest.mark.parametrize(
    "pull_request",
    [
        {"reviews": None},
        {},
        {"reviews": {"nodes": None}},
        {"reviews": {"nodes": [None]}},
    ],
    ids=["null-reviews", "missing-reviews", "null-nodes", "null-review"],
)
def test_reviews_github_failed_to_read_are_refused_not_taken_for_none(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, pull_request: dict[str, Any]
) -> None:
    """A partial answer, such as one with an error on reviews, must not read as no review."""
    answer = {"id": "PR_1", "headRefOid": HEAD, "baseRefOid": BASE, **pull_request}
    monkeypatch.setattr(pending_reviews, "pull_request_node", lambda *_args: answer)

    with pytest.raises(PullRequestError, match="couldn't read your pending review"):
        _read()
    with pytest.raises(PullRequestError, match="couldn't read your pending review"):
        pending_reviews.own_pending_review("/workspace", PR)
    assert fake.graphql_reads == []


def test_a_kept_answer_stands_in_until_the_review_moves(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(fake, FakeGitHub(3))
    first = _read()
    reads = len(fake.graphql_reads)

    assert _read() == first
    assert len(fake.graphql_reads) == reads  # only the small review read went out

    fake.count_override = 4
    _read()
    assert len(fake.graphql_reads) > reads

    reads = len(fake.graphql_reads)
    clock = pending_reviews.time.monotonic()
    monkeypatch.setattr(
        pending_reviews.time, "monotonic", lambda: clock + pending_reviews.CACHE_SECONDS + 1
    )
    _read()
    assert len(fake.graphql_reads) > reads


def test_a_pending_write_drops_the_kept_answer_even_when_it_fails(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(fake, FakeGitHub(3))
    _read()
    assert pending_reviews._cache

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PullRequestError("GitHub didn't answer.")

    monkeypatch.setattr(github_reviews, "mutate", refuse)
    # The host's active account is signed in, so the change gets as far as GitHub.
    monkeypatch.setattr(github_resource, "_gh_active_token", lambda *_args, **_kwargs: "token")
    with pytest.raises(PullRequestError):
        github_reviews.discard_pending_review("/workspace", PR, pending_review_id=REVIEW)

    assert pending_reviews._cache == {}


def test_a_read_that_overlapped_a_change_is_not_kept(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft edit during a read would leave the old text kept for the whole cache window."""
    _use(fake, FakeGitHub(3))
    read_threads = fake.graphql

    def edit_mid_read(*args: Any, **kwargs: Any) -> Any:
        pending_reviews.forget_pending_review(PR)
        return read_threads(*args, **kwargs)

    monkeypatch.setattr(pending_reviews, "_graphql", edit_mid_read)
    _read()

    assert pending_reviews._cache == {}


def test_a_read_after_a_change_is_kept_again(fake: FakeGitHub) -> None:
    _use(fake, FakeGitHub(3))
    pending_reviews.forget_pending_review(PR)
    _read()
    assert list(pending_reviews._cache) == [(PR.url, REVIEW)]


def test_expired_answers_for_other_pull_requests_are_dropped(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_reviews._cache[("https://github.com/example-org/example-repo/pull/9", "PRR_old")] = (
        (),
        pending_reviews.time.monotonic() - pending_reviews.CACHE_SECONDS - 1,
        {},
    )
    _use(fake, FakeGitHub(1))
    _read()
    assert list(pending_reviews._cache) == [(PR.url, REVIEW)]


def test_a_null_comment_in_a_thread_is_skipped(fake: FakeGitHub) -> None:
    github = _use(fake, FakeGitHub(1))
    github.threads[0]["comments"]["nodes"].insert(0, None)

    answer = _read()

    [thread] = answer["threads"]
    assert [draft["id"] for draft in thread["comments"]["data"]] == ["PRRC_0"]
    assert thread["diff_hunk"] == "@@ -1 +1 @@"
