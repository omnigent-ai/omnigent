"""The viewer's pending review on a pull request: finding it, and reading it with its drafts.

Every pending-review path finds the review with :func:`own_pending_review`: the PR's latest
pending review, only where the viewer wrote it. The read answers the page's
``session.github.pr_pending_review`` shape: the review's own comments are the list and the
count, each placed in its thread from the PR's review threads, and the rest ``unplaced``.
It is bounded: comments and threads are read 100 at a time up to a page limit, each thread's
diff hunk is cut at :data:`MAX_DIFF_HUNK_BYTES`, and the whole answer at
:data:`MAX_ANSWER_BYTES`. Anything cut off makes ``complete`` false; less is never all.
Answers are kept per review and reused while one small read says nothing moved.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from omnigent.runner.github_graphql import pull_request_node
from omnigent.runner.github_review_fields import NODE_ID, SIDES
from omnigent.runner.github_review_threads import _graphql
from omnigent.runner.pull_request_errors import PullRequestError
from omnigent.runner.session_prs import PullRequestRef

PAGE_SIZE = 100
# Enough for any review a person writes, and a bound on the reads one answer makes.
MAX_DRAFT_PAGES = 30
MAX_THREAD_PAGES = 30
DRAFT_COMMENTS_PER_THREAD = 50
MAX_DIFF_HUNK_BYTES = 8 * 1024
MAX_ANSWER_BYTES = 4 * 1024 * 1024
# How long a kept answer may stand in, even while nothing seems to move.
CACHE_SECONDS = 60.0

_SUBJECT_TYPES = frozenset({"LINE", "FILE"})
NO_PENDING_REVIEW = "You have no pending review on this pull request. Start one first."
_UNREADABLE = "GitHub couldn't read your pending review. Refresh to retry."

# The PR's heads and the viewer's pending review: what finds it, and what says it moved.
OWN_PENDING_REVIEW = (
    "id headRefOid baseRefOid reviews(last: 1, states: [PENDING]) {"
    " nodes { id body viewerDidAuthor commit { oid } comments { totalCount } } }"
)

_COMMENTS_QUERY = """query($id: ID!, $first: Int!, $after: String) {
  node(id: $id) { ... on PullRequestReview {
    id state viewerDidAuthor
    comments(first: $first, after: $after) {
      totalCount pageInfo { hasNextPage endCursor }
      nodes { id body path line startLine originalLine originalStartLine outdated createdAt url
        state replyTo { id } }
    }
  } }
}"""

# A draft and its thread as GitHub names them, for the read and for an add's answer.
DRAFT_COMMENT_FIELDS = (
    "id author { login } body createdAt url state diffHunk"
    " viewerCanUpdate viewerCanDelete viewerCanReact viewerDidAuthor pullRequestReview { id }"
)
DRAFT_THREAD_FIELDS = (
    "id path diffSide line startLine startDiffSide originalLine originalStartLine"
    " subjectType isResolved isOutdated viewerCanReply viewerCanResolve viewerCanUnresolve"
)

_THREADS_QUERY = (
    """query(
  $owner: String!, $name: String!, $number: Int!, $first: Int!, $drafts: Int!, $after: String
) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    headRefOid baseRefOid
    reviewThreads(first: $first, after: $after) {
      nodes { """
    + DRAFT_THREAD_FIELDS
    + " comments(last: $drafts) { nodes { "
    + DRAFT_COMMENT_FIELDS
    + """ } } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}"""
)


def own_pending_node(pull_request: dict[str, Any]) -> dict[str, Any] | None:
    """The PR's latest pending review, only where the viewer wrote it.

    A reviews connection GitHub failed to read is refused, not taken for no review.
    """
    reviews = pull_request.get("reviews")
    nodes = reviews.get("nodes") if isinstance(reviews, dict) else None
    if not isinstance(nodes, list) or (nodes and not isinstance(nodes[0], dict)):
        raise PullRequestError(_UNREADABLE)
    review = nodes[0] if nodes else None
    if not review or not isinstance(review.get("id"), str) or not review["id"]:
        return None
    return review if review.get("viewerDidAuthor") is True else None


def own_pending_review(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """The viewer's pending review on this pull request, or a refusal before any write."""
    review = own_pending_node(pull_request_node(root, reference, OWN_PENDING_REVIEW))
    if review is None:
        raise PullRequestError(NO_PENDING_REVIEW)
    return review


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _line(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _opaque(value: Any) -> str | None:
    return value if isinstance(value, str) and NODE_ID.fullmatch(value) else None


def _capped_hunk(hunk: str) -> tuple[str, bool]:
    """``hunk`` within :data:`MAX_DIFF_HUNK_BYTES`, cut at a line where it can be."""
    encoded = hunk.encode("utf-8")
    if len(encoded) <= MAX_DIFF_HUNK_BYTES:
        return hunk, False
    cut = encoded[:MAX_DIFF_HUNK_BYTES].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    return (cut[:newline] if newline > 0 else cut), True


def _review_comment(node: Any) -> dict[str, Any] | None:
    """One of the review's own comments as the page lists a draft; null when unreadable."""
    if not isinstance(node, dict) or (draft_id := _opaque(node.get("id"))) is None:
        return None
    return {
        "id": draft_id,
        "body": _text(node.get("body")) or "",
        "path": _text(node.get("path")),
        "line": _line(node.get("line")),
        "start_line": _line(node.get("startLine")),
        "original_line": _line(node.get("originalLine")),
        "original_start_line": _line(node.get("originalStartLine")),
        "outdated": node.get("outdated") is True,
        "created_at": _text(node.get("createdAt")),
        "url": _text(node.get("url")),
        "reply_to_id": _opaque((node.get("replyTo") or {}).get("id")),
    }


def _review_comments(
    root: str, reference: PullRequestRef, review_id: str
) -> tuple[list[dict[str, Any]], int, bool]:
    """The review's comments, GitHub's count of them, and whether every one was read."""
    comments: dict[str, dict[str, Any]] = {}
    total: int | None = None
    after: str | None = None
    for _ in range(MAX_DRAFT_PAGES):
        response = _graphql(
            root, reference, _COMMENTS_QUERY, id=review_id, first=PAGE_SIZE, after=after
        )
        review = (response.get("data") or {}).get("node")
        if (
            not isinstance(review, dict)
            or review.get("id") != review_id
            or review.get("state") != "PENDING"
            or review.get("viewerDidAuthor") is not True
        ):
            raise PullRequestError(_UNREADABLE)
        connection = review.get("comments")
        if not isinstance(connection, dict) or not isinstance(connection.get("totalCount"), int):
            raise PullRequestError(_UNREADABLE)
        if total is None:
            total = connection["totalCount"]
        elif total != connection["totalCount"]:
            # Drafts came or went between pages: what was read isn't one list.
            return list(comments.values()), total, False
        for node in connection.get("nodes") or []:
            comment = _review_comment(node)
            if comment and comment["id"] not in comments:
                comments[comment["id"]] = comment
        page = connection.get("pageInfo") or {}
        after = _text(page.get("endCursor"))
        if page.get("hasNextPage") is not True:
            return list(comments.values()), total, len(comments) == total
        if not after:
            break
    return list(comments.values()), total or 0, False


def _draft(comment: Any, review_id: str, reply_to: dict[str, str | None]) -> dict[str, Any] | None:
    if not isinstance(comment, dict) or comment.get("state") != "PENDING":
        return None
    if (comment.get("pullRequestReview") or {}).get("id") != review_id:
        return None
    draft_id = _opaque(comment.get("id"))
    if draft_id is None or draft_id not in reply_to:
        return None
    return {
        "id": draft_id,
        "author": _text((comment.get("author") or {}).get("login")),
        "body": _text(comment.get("body")) or "",
        "minimized": False,
        "created_at": _text(comment.get("createdAt")),
        "url": _text(comment.get("url")),
        "pending": True,
        "reply_to_id": reply_to[draft_id],
        "can_update": comment.get("viewerCanUpdate") is True,
        "can_delete": comment.get("viewerCanDelete") is True,
        "can_react": comment.get("viewerCanReact") is True,
        "viewer_did_author": comment.get("viewerDidAuthor") is True,
    }


def _draft_thread(
    thread: Any, review_id: str, reply_to: dict[str, str | None]
) -> dict[str, Any] | None:
    """``thread`` with only the review's listed drafts, in the thread read's shape; or None."""
    if not isinstance(thread, dict) or (thread_id := _opaque(thread.get("id"))) is None:
        return None
    nodes = (thread.get("comments") or {}).get("nodes") or []
    drafts = [draft for node in nodes if (draft := _draft(node, review_id, reply_to))]
    if not drafts:
        return None
    hunk = next(
        (
            node["diffHunk"]
            for node in nodes
            if isinstance(node, dict) and _text(node.get("diffHunk"))
        ),
        "",
    )
    diff_hunk, truncated = _capped_hunk(hunk)
    return {
        "id": thread_id,
        "path": _text(thread.get("path")) or "",
        "side": thread.get("diffSide") if thread.get("diffSide") in SIDES else "RIGHT",
        "line": _line(thread.get("line")),
        "start_line": _line(thread.get("startLine")),
        "start_side": thread.get("startDiffSide")
        if thread.get("startDiffSide") in SIDES
        else None,
        "original_line": _line(thread.get("originalLine")),
        "original_start_line": _line(thread.get("originalStartLine")),
        "subject_type": (
            thread.get("subjectType") if thread.get("subjectType") in _SUBJECT_TYPES else "LINE"
        ),
        "resolved": thread.get("isResolved") is True,
        "can_resolve": thread.get("viewerCanResolve") is True,
        "can_unresolve": thread.get("viewerCanUnresolve") is True,
        "outdated": thread.get("isOutdated") is True,
        "can_reply": thread.get("viewerCanReply") is True,
        "diff_hunk": diff_hunk,
        "diff_hunk_truncated": truncated,
        "comments": {"data": drafts, "has_more": False, "next_cursor": None},
    }


def new_draft_thread(thread: Any, review_id: str) -> dict[str, Any] | None:
    """A conversation an add just made, holding only its one draft in ``review_id``; or None."""
    nodes = ((thread or {}).get("comments") or {}).get("nodes") or []
    ids: dict[str, str | None] = {
        node["id"]: None for node in nodes if isinstance(node, dict) and _opaque(node.get("id"))
    }
    shaped = _draft_thread(thread, review_id, ids)
    return shaped if shaped and len(shaped["comments"]["data"]) == 1 else None


def new_draft_reply(comment: Any, review_id: str) -> dict[str, Any] | None:
    """A reply an add just made, as a draft in ``review_id``; or None."""
    draft_id = _opaque((comment or {}).get("id")) if isinstance(comment, dict) else None
    shaped = _draft(comment, review_id, {draft_id: None}) if draft_id else None
    if shaped:
        del shaped["reply_to_id"]
    return shaped


class _HeadMoved(Exception):
    """A thread page was read at another head or base than the review's."""


def _placed(
    root: str,
    reference: PullRequestRef,
    review_id: str,
    comments: list[dict[str, Any]],
    heads: tuple[str | None, str | None],
) -> tuple[list[dict[str, Any]], bool]:
    """The threads holding ``comments``, and whether every thread page needed was read."""
    reply_to = {comment["id"]: comment["reply_to_id"] for comment in comments}
    owner, name = reference.repository.split("/", 1)
    threads: list[dict[str, Any]] = []
    placed: set[str] = set()
    after: str | None = None
    for _ in range(MAX_THREAD_PAGES):
        if len(placed) == len(reply_to):
            return threads, True
        response = _graphql(
            root,
            reference,
            _THREADS_QUERY,
            owner=owner,
            name=name,
            number=reference.number,
            first=PAGE_SIZE,
            drafts=DRAFT_COMMENTS_PER_THREAD,
            after=after,
        )
        pull_request = ((response.get("data") or {}).get("repository") or {}).get("pullRequest")
        if not isinstance(pull_request, dict):
            raise PullRequestError(_UNREADABLE)
        connection = pull_request.get("reviewThreads")
        if not isinstance(connection, dict):
            raise PullRequestError(_UNREADABLE)
        if (pull_request.get("headRefOid"), pull_request.get("baseRefOid")) != heads:
            raise _HeadMoved
        for node in connection.get("nodes") or []:
            thread = _draft_thread(node, review_id, reply_to)
            if thread is None:
                continue
            threads.append(thread)
            placed.update(draft["id"] for draft in thread["comments"]["data"])
        page = connection.get("pageInfo") or {}
        after = _text(page.get("endCursor"))
        if page.get("hasNextPage") is not True or not after:
            return threads, True
    return threads, len(placed) == len(reply_to)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value).encode("utf-8"))


# What each further item in a JSON list adds besides itself: the ", " before it.
_ITEM_SEPARATOR_BYTES = 2


def _within_answer_cap(answer: dict[str, Any]) -> dict[str, Any]:
    """``answer`` cut to :data:`MAX_ANSWER_BYTES`, dropping its last drafts, said incomplete.

    Each thread and draft is measured once, and the drafts that fit are kept in one pass.
    """
    review = answer["review"]
    # The answer around its drafts, with the widest count its review could then say.
    frame = {
        **answer,
        "threads": [],
        "unplaced": [],
        "complete": False,
        "review": {**review, "read_count": review["comment_count"]},
    }
    room = MAX_ANSWER_BYTES - _json_bytes(frame)
    threads: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    fits = True
    for thread in answer["threads"]:
        shell = {**thread, "comments": {**thread["comments"], "data": []}}
        cost = _json_bytes(shell) + _ITEM_SEPARATOR_BYTES
        drafts: list[dict[str, Any]] = []
        for draft in thread["comments"]["data"]:
            draft_cost = _json_bytes(draft) + _ITEM_SEPARATOR_BYTES
            if cost + draft_cost > room:
                fits = False
                break
            cost += draft_cost
            drafts.append(draft)
        if drafts:
            threads.append({**shell, "comments": {**shell["comments"], "data": drafts}})
            room -= cost
        if not fits:
            break
    for draft in answer["unplaced"] if fits else []:
        draft_cost = _json_bytes(draft) + _ITEM_SEPARATOR_BYTES
        if draft_cost > room:
            fits = False
            break
        room -= draft_cost
        unplaced.append(draft)
    if fits:
        return answer
    kept = sum(len(thread["comments"]["data"]) for thread in threads) + len(unplaced)
    return {
        **frame,
        "threads": threads,
        "unplaced": unplaced,
        "review": {**review, "read_count": kept},
    }


# Answers kept per review: (pr url, review id) → (what says it moved, when read, answer).
_cache: dict[tuple[str, str], tuple[tuple[Any, ...], float, dict[str, Any]]] = {}
# Bumped by every change to a PR's pending review, so a read that overlapped one isn't kept.
_generations: dict[str, int] = {}
_cache_lock = threading.Lock()


def forget_pending_review(reference: PullRequestRef) -> None:
    """Drop the kept answers for ``reference``'s pending review, after a change to it."""
    with _cache_lock:
        _generations[reference.url] = _generations.get(reference.url, 0) + 1
        for key in [key for key in _cache if key[0] == reference.url]:
            del _cache[key]


def _keep(key: tuple[str, str], generation: int, entry: tuple[Any, ...]) -> None:
    """Keep a read's answer unless a change began since it started; drop expired answers."""
    now = time.monotonic()
    with _cache_lock:
        for expired in [old for old, kept in _cache.items() if now - kept[1] >= CACHE_SECONDS]:
            del _cache[expired]
        if _generations.get(key[0], 0) == generation:
            _cache[key] = entry  # type: ignore[assignment]


def get_pending_review(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """The viewer's pending review on the PR with its drafts, as the page reads it."""
    with _cache_lock:
        generation = _generations.get(reference.url, 0)
    pull_request = pull_request_node(root, reference, OWN_PENDING_REVIEW)
    heads = (_text(pull_request.get("headRefOid")), _text(pull_request.get("baseRefOid")))
    answer: dict[str, Any] = {
        "object": "session.github.pr_pending_review",
        "head_sha": heads[0],
        "base_sha": heads[1],
    }
    review = own_pending_node(pull_request)
    if review is None:
        return {**answer, "review": None, "threads": [], "unplaced": [], "complete": True}
    review_id = review["id"]
    moved = (heads, review.get("body"), (review.get("comments") or {}).get("totalCount"))
    with _cache_lock:
        kept = _cache.get((reference.url, review_id))
    if kept and kept[0] == moved and time.monotonic() - kept[1] < CACHE_SECONDS:
        return kept[2]
    comments, total, complete = _review_comments(root, reference, review_id)
    try:
        threads, all_pages = _placed(root, reference, review_id, comments, heads)
    except _HeadMoved:
        # A push while it read: its lines are another diff's; the next read places them.
        threads, all_pages = [], False
    placed = {draft["id"] for thread in threads for draft in thread["comments"]["data"]}
    result = _within_answer_cap(
        {
            **answer,
            "review": {
                "id": review_id,
                "body": _text(review.get("body")) or "",
                "commit_oid": _text((review.get("commit") or {}).get("oid")),
                "comment_count": total,
                "read_count": len(comments),
            },
            "threads": threads,
            "unplaced": [comment for comment in comments if comment["id"] not in placed],
            "complete": complete and all_pages,
        }
    )
    _keep((reference.url, review_id), generation, (moved, time.monotonic(), result))
    return result
