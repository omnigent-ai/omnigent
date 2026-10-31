"""Published GitHub review conversations and inline comments."""

from __future__ import annotations

import json
import time
from typing import Any

from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef

_COMMENT_FIELDS = """
  id fullDatabaseId author { login } body createdAt url isMinimized state
  diffHunk originalLine originalStartLine
"""
_PAGE_FIELDS = "pageInfo { hasNextPage endCursor }"
_THREAD_FIELDS = f"""
  id path diffSide line startLine startDiffSide originalLine originalStartLine
  subjectType isResolved isOutdated viewerCanReply
  comments(first: 20) {{ nodes {{ {_COMMENT_FIELDS} }} {_PAGE_FIELDS} }}
"""


def _graphql(root: str, reference: PullRequestRef, query: str, **variables: Any) -> dict[str, Any]:
    """Keep partial GraphQL data, including when gh exits nonzero for field errors."""
    host = github._host_args(root, reference)
    token = github._pr_token(root, reference)
    if github._gh_timed_out.get():
        raise ValueError("GitHub authentication timed out. Refresh to retry.")
    _, output, _ = github._pr_json_write(
        root,
        host=host,
        token=token,
        method="POST",
        endpoint="graphql",
        fields={"query": query, "variables": variables},
    )
    try:
        response = json.loads(output)
    except ValueError as exc:
        raise ValueError("GitHub could not load review conversations. Refresh to retry.") from exc
    if not isinstance(response, dict):
        raise ValueError("GitHub returned an unexpected review response.")
    return response


def _nodes(connection: Any) -> list[dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    return [node for node in connection.get("nodes") or [] if isinstance(node, dict)]


def _page(connection: Any) -> dict[str, Any]:
    page = connection.get("pageInfo") if isinstance(connection, dict) else None
    if not isinstance(page, dict):
        return {"has_more": False, "next_cursor": None, "warning": "Conversations are incomplete."}
    return {"has_more": bool(page.get("hasNextPage")), "next_cursor": page.get("endCursor")}


def _comment(comment: dict[str, Any]) -> dict[str, Any] | None:
    if comment.get("state") == "PENDING" or not comment.get("id"):
        return None
    author = comment.get("author")
    minimized = bool(comment.get("isMinimized"))
    return {
        "id": str(comment["id"]),
        "author": author.get("login") if isinstance(author, dict) else None,
        "body": "" if minimized else comment.get("body") or "",
        "minimized": minimized,
        "created_at": comment.get("createdAt"),
        "url": comment.get("url"),
    }


def _thread(thread: dict[str, Any]) -> dict[str, Any] | None:
    raw_comments = _nodes(thread.get("comments"))
    comments = [value for raw in raw_comments if (value := _comment(raw)) is not None]
    if not thread.get("id") or not comments:
        return None
    return {
        "id": str(thread["id"]),
        "path": thread.get("path"),
        "side": thread.get("diffSide"),
        "line": thread.get("line"),
        "start_line": thread.get("startLine"),
        "start_side": thread.get("startDiffSide"),
        "original_line": thread.get("originalLine"),
        "original_start_line": thread.get("originalStartLine"),
        "subject_type": thread.get("subjectType"),
        "resolved": bool(thread.get("isResolved")),
        "outdated": bool(thread.get("isOutdated")),
        "can_reply": bool(thread.get("viewerCanReply")),
        "diff_hunk": raw_comments[0].get("diffHunk") or "",
        "comments": {"data": comments, **_page(thread.get("comments"))},
    }


def _threads(pr: dict[str, Any]) -> dict[str, Any]:
    connection = pr.get("reviewThreads")
    return {
        "data": [value for raw in _nodes(connection) if (value := _thread(raw)) is not None],
        "head_sha": pr.get("headRefOid"),
        "base_sha": pr.get("baseRefOid"),
        **_page(connection),
    }


def read_threads(
    root: str,
    reference: PullRequestRef,
    *,
    after: str = "",
    head_sha: str = "",
    base_sha: str = "",
) -> dict[str, Any]:
    """Read published threads; pagination must match the displayed revision."""
    owner, name = reference.repository.split("/", 1)
    query = f"""query($owner: String!, $name: String!, $number: Int!, $after: String) {{
      repository(owner: $owner, name: $name) {{ pullRequest(number: $number) {{
        headRefOid baseRefOid reviewThreads(first: 25, after: $after) {{
          nodes {{ {_THREAD_FIELDS} }} {_PAGE_FIELDS}
        }}
      }} }}
    }}"""
    response = _graphql(
        root,
        reference,
        query,
        owner=owner,
        name=name,
        number=reference.number,
        after=after or None,
    )
    pr = ((response.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not isinstance(pr, dict):
        raise ValueError("GitHub could not load review conversations.")
    if after and (pr.get("headRefOid") != head_sha or pr.get("baseRefOid") != base_sha):
        raise ValueError("The pull request changed; refresh before loading more conversations.")
    result = _threads(pr)
    if response.get("errors"):
        result["warning"] = "GitHub could not load all review conversations."
    return result


def _read_thread(
    root: str, reference: PullRequestRef, thread_id: str, *, after: str | None = None
) -> dict[str, Any]:
    query = f"""query($id: ID!, $after: String) {{
      node(id: $id) {{ ... on PullRequestReviewThread {{
        id viewerCanReply pullRequest {{ url }}
        root: comments(first: 1) {{ nodes {{ fullDatabaseId state }} }}
        comments(first: 20, after: $after) {{ nodes {{ {_COMMENT_FIELDS} }} {_PAGE_FIELDS} }}
      }} }}
    }}"""
    response = _graphql(root, reference, query, id=thread_id, after=after)
    thread = (response.get("data") or {}).get("node")
    pr = thread.get("pullRequest") if isinstance(thread, dict) else None
    if (
        response.get("errors")
        or not isinstance(thread, dict)
        or not isinstance(pr, dict)
        or not isinstance(pr.get("url"), str)
        or pr["url"].casefold() != reference.url.casefold()
    ):
        raise ValueError("This review conversation is unavailable on the selected pull request.")
    return thread


def read_comments(
    root: str, reference: PullRequestRef, *, thread_id: str, after: str
) -> dict[str, Any]:
    """Page comments after checking that the thread belongs to the selected PR."""
    thread = _read_thread(root, reference, thread_id, after=after)
    connection = thread.get("comments")
    return {
        "data": [value for raw in _nodes(connection) if (value := _comment(raw)) is not None],
        **_page(connection),
    }


def reply(root: str, reference: PullRequestRef, *, thread_id: str, body: str) -> dict[str, Any]:
    """Reply once to the published root, using the canonical write response."""
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        thread = _read_thread(root, reference, thread_id)
        roots = _nodes(thread.get("root"))
        root_comment = roots[0] if roots else {}
        root_id = str(root_comment.get("fullDatabaseId") or "")
        if not thread.get("viewerCanReply") or root_comment.get("state") != "SUBMITTED":
            raise ValueError("The GitHub account cannot reply to this conversation.")
        if not root_id.isdecimal():
            raise ValueError("GitHub did not return the conversation's original comment.")
        result = github._post_comment(
            root,
            reference,
            endpoint=f"repos/{reference.repository}/pulls/{reference.number}/comments/{root_id}/replies",
            body=body,
        )
    author = result.get("user")
    return {
        "object": "session.github.review_reply",
        "thread_id": thread_id,
        "comment": {
            "id": result.get("node_id") or str(result["id"]),
            "author": author.get("login") if isinstance(author, dict) else None,
            "body": result.get("body") or "",
            "created_at": result.get("created_at"),
            "url": result["html_url"],
            "minimized": False,
        },
    }


def create_thread(
    root: str, reference: PullRequestRef, *, body: str, anchor: dict[str, Any]
) -> dict[str, Any]:
    """Submit exactly one new conversation, without modifying an existing draft review."""
    uncertain = "Could not confirm comment posting. Check GitHub before retrying."
    owner, name = reference.repository.split("/", 1)
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        response = _graphql(
            root,
            reference,
            """query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) { id headRefOid baseRefOid }
          }
        }""",
            owner=owner,
            name=name,
            number=reference.number,
        )
        pr = ((response.get("data") or {}).get("repository") or {}).get("pullRequest")
        if response.get("errors") or not isinstance(pr, dict) or not pr.get("id"):
            raise ValueError("GitHub could not load this pull request. Your comment was not sent.")
        if (
            pr.get("headRefOid") != anchor["head_sha"]
            or pr.get("baseRefOid") != anchor["base_sha"]
        ):
            raise ValueError(
                "The pull request changed. Refresh and reselect the lines before posting."
            )
        thread = {
            "path": anchor["path"],
            "line": anchor["line"],
            "side": anchor["side"],
            "body": body,
        }
        if anchor["start_line"] is not None:
            thread.update(startLine=anchor["start_line"], startSide=anchor["side"])
        fields = _THREAD_FIELDS.replace("comments(first: 20)", "comments(first: 1)")
        query = f"""mutation($input: AddPullRequestReviewInput!) {{
          addPullRequestReview(input: $input) {{ pullRequestReview {{
            state comments(first: 1) {{ nodes {{ id }} }}
            pullRequest {{ reviewThreads(last: 25) {{ nodes {{ {fields} }} }} }}
          }} }}
        }}"""
        host, token = github._host_args(root, reference), github._pr_token(root, reference)
        if github._gh_timed_out.get():
            raise ValueError("GitHub authentication timed out. Your comment was not sent.")
        _, output, _ = github._pr_json_write(
            root,
            host=host,
            token=token,
            method="POST",
            endpoint="graphql",
            fields={
                "query": query,
                "variables": {
                    "input": {
                        "pullRequestId": pr["id"],
                        "commitOID": anchor["head_sha"],
                        "event": "COMMENT",
                        "threads": [thread],
                    }
                },
            },
        )
        try:
            response = json.loads(output)
        except ValueError as exc:
            raise ValueError(uncertain) from exc
        if not isinstance(response, dict):
            raise ValueError(uncertain)
        review = ((response.get("data") or {}).get("addPullRequestReview") or {}).get(
            "pullRequestReview"
        ) or {}
        roots = _nodes(review.get("comments"))
        if review.get("state") == "COMMENTED" and roots:
            for raw in _nodes((review.get("pullRequest") or {}).get("reviewThreads")):
                shaped = _thread(raw)
                if shaped and shaped["comments"]["data"][0]["id"] == roots[0].get("id"):
                    return {"object": "session.github.review_thread", "thread": shaped}
        raise ValueError(uncertain)
