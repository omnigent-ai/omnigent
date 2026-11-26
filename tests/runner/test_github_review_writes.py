"""GitHub review and comment writes, against a fake ``gh`` and GraphQL endpoint.

Each write refuses a moved head before any mutation is sent, sends the token only in the
environment, reports a refusal with its reason, and reports a write GitHub never
confirmed as an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from omnigent.runner import github_failures, pr_actions, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import (
    PULL_REQUEST_CHANGED_MESSAGE,
    CommentChangedError,
    InvalidPullRequestInput,
    PullRequestChangedError,
    PullRequestError,
    UnknownOutcomeError,
)
from omnigent.runner.session_prs import PullRequestRef

TOKEN = "ghp_example_token_value"
# The token lookup FakeGitHub replaces, for tests of how an action picks its account.
_REAL_PR_TOKEN = github._pr_token
HEAD = "66331978a046d81e2f1a6772e6354f9ae43b734b"
MOVED = "1" * 40
BASE = "2" * 40
PR_URL = "https://github.com/example-org/example-repo/pull/7"
COMMENT_PATH = "repos/example-org/example-repo/issues/comments/5"
REVIEW_COMMENT_PATH = "repos/example-org/example-repo/pulls/comments/5"
COMMENT_PATHS = (COMMENT_PATH, REVIEW_COMMENT_PATH)


def _comment_link(kind: str, number: int, repository: str = "example-org/example-repo") -> str:
    """The API link a comment carries, naming the pull request it is on."""
    collection = "issues" if kind == "issue" else "pulls"
    return f"https://api.github.com/repos/{repository}/{collection}/{number}"


def _new_draft(comment_id: str, body: str) -> dict[str, Any]:
    """A draft GitHub just made in the viewer's pending review R_1, as its mutation answers."""
    return {
        "id": comment_id,
        "author": {"login": "viewer"},
        "body": body,
        "createdAt": "2026-11-24T10:00:00Z",
        "url": f"{PR_URL}#discussion_r1",
        "state": "PENDING",
        "diffHunk": "@@ -1 +1 @@",
        "viewerCanUpdate": True,
        "viewerCanDelete": True,
        "viewerCanReact": False,
        "viewerDidAuthor": True,
        "pullRequestReview": {"id": "R_1"},
    }


class FakeGitHub:
    """Answers ``gh`` calls and GraphQL requests for the pull requests it holds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.heads = {7: HEAD}
        self.pending: list[dict[str, Any]] = []
        self.gh_calls: list[tuple[list[str], str | None]] = []
        self.json_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.comment: dict[str, Any] = {
            "body": "Original text",
            "node_id": "IC_1",
            "issue_url": _comment_link("issue", 7),
            "pull_request_url": _comment_link("review", 7),
        }
        # Thread id to the pull request number and repository it belongs to.
        self.threads: dict[str, tuple[int, str]] = {"T_1": (7, "example-org/example-repo")}
        # Whether the host's account wrote the comment, as GitHub's viewerDidAuthor says.
        self.viewer_wrote_comment = True
        self.refused: dict[str, tuple[str, str | None]] = {}
        # A failed send: (exit code, stdout, stderr) for the next write to that endpoint kind.
        self.graphql_failure: tuple[int, str, str] | None = None
        self.rest_failure: tuple[int, str, str] | None = None
        # Mutations GitHub runs but answers with errors in a field they selected.
        self.errors_with_payload: set[str] = set()
        # Where the head moves when a sent mutation fails, as a push racing it would.
        self.head_after_failure: str | None = None
        monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: TOKEN)
        monkeypatch.setattr(github, "_host_args", lambda _root, ref: ["--hostname", ref.host])
        monkeypatch.setattr(github, "_gh", self._gh)
        monkeypatch.setattr(github, "_pr_json_write", self._json_write)
        monkeypatch.setattr(
            pr_resource, "_selected_pr", lambda _session, url: PullRequestRef.from_url(url)
        )

    def mutations(self, name: str) -> list[dict[str, Any]]:
        return [
            fields["variables"]
            for _method, endpoint, fields in self.json_calls
            if endpoint == "graphql" and name in fields["query"]
        ]

    def refuse_mutation(self, name: str, message: str, *, head_becomes: str | None = None) -> None:
        """Refuse the named GraphQL mutation with ``message``, moving the head if asked."""
        self.refused[name] = (message, head_becomes)

    def _move_head(self, head_becomes: str | None) -> None:
        if head_becomes is not None:
            self.heads[7] = head_becomes

    def _node(self, number: int) -> dict[str, Any]:
        return {
            "id": f"PR_{number}",
            "headRefOid": self.heads[number],
            "baseRefOid": BASE,
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "reviews": {"nodes": self.pending},
        }

    def _draft_page(self, after: str | None) -> dict[str, Any]:
        """One page of the pending review's drafts, 100 at a time as GitHub pages them."""
        drafts = self.pending[0]["comments"]["nodes"] if self.pending else []
        start = int(after) if after else 0
        end = start + 100
        return {
            "nodes": drafts[start:end],
            "pageInfo": {"hasNextPage": end < len(drafts), "endCursor": str(end)},
        }

    def _thread(self, thread_id: str) -> dict[str, Any] | None:
        if thread_id not in self.threads:
            return None
        number, repository = self.threads[thread_id]
        return {"pullRequest": {"number": number, "repository": {"nameWithOwner": repository}}}

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "on PullRequestReview {" in query and "node(id" in query:
            return {"data": {"node": {"comments": self._draft_page(variables.get("after"))}}}
        if "viewerDidAuthor" in query and "node(id" in query:
            return {"data": {"node": {"viewerDidAuthor": self.viewer_wrote_comment}}}
        if "node(id" in query:
            return {"data": {"node": self._thread(variables["threadId"])}}
        if "mutation" not in query:
            number = variables.get("number", 7)
            return {"data": {"repository": {"pullRequest": self._node(number)}}}
        for name, (message, head_becomes) in self.refused.items():
            if name in query:
                self._move_head(head_becomes)
                return {"errors": [{"message": message}]}
        if "addPullRequestReview(" in query:
            event = variables["input"].get("event", "COMMENT")
            states = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
            state = states.get(event, "COMMENTED" if "event" in variables["input"] else "PENDING")
            review = {
                "id": "R_1",
                "state": state,
                "viewerDidAuthor": True,
                "commit": {"oid": variables["input"].get("commitOID", HEAD)},
            }
            return {"data": {"addPullRequestReview": {"pullRequestReview": review}}}
        if "submitPullRequestReview" in query:
            review = {
                "id": "R_1",
                "state": "COMMENTED",
                "body": variables["input"]["body"],
                "createdAt": "2026-11-24T10:00:00Z",
                "url": f"{PR_URL}#pullrequestreview-1",
                "commit": {"oid": HEAD},
            }
            return {"data": {"submitPullRequestReview": {"pullRequestReview": review}}}
        if "deletePullRequestReviewComment" in query:
            payload = {"pullRequestReview": {"id": "R_1"}}
            return {"data": {"deletePullRequestReviewComment": payload}}
        if "updatePullRequestReviewComment" in query:
            draft = variables["input"]
            edited = {"id": draft["pullRequestReviewCommentId"], "body": draft["body"]}
            return {
                "data": {"updatePullRequestReviewComment": {"pullRequestReviewComment": edited}}
            }
        if "addPullRequestReviewThreadReply" in query:
            comment = _new_draft("PRRC_new", variables["input"]["body"])
            return {"data": {"addPullRequestReviewThreadReply": {"comment": comment}}}
        if "addPullRequestReviewThread(" in query:
            draft = variables["input"]
            thread = {
                "id": "T_new",
                "path": draft["path"],
                "diffSide": draft["side"],
                "line": draft["line"],
                "startLine": draft.get("startLine"),
                "subjectType": "LINE",
                "viewerCanReply": True,
                "comments": {"nodes": [_new_draft("PRRC_line", draft["body"])]},
            }
            return {"data": {"addPullRequestReviewThread": {"thread": thread}}}
        if "deletePullRequestReview" in query:
            return {"data": {"deletePullRequestReview": {"pullRequestReview": {"id": "R_1"}}}}
        if "unresolveReviewThread" in query:
            reopened = {"thread": {"id": "T_1", "isResolved": False}}
            return {"data": {"unresolveReviewThread": reopened}}
        if "resolveReviewThread" in query:
            return {"data": {"resolveReviewThread": {"thread": {"id": "T_1", "isResolved": True}}}}
        if "addReaction" in query:
            subject = {
                "viewerCanReact": True,
                "reactionGroups": [
                    {
                        "content": "THUMBS_UP",
                        "viewerHasReacted": True,
                        "reactors": {"totalCount": 2},
                    },
                    {"content": "HEART", "viewerHasReacted": False, "reactors": {"totalCount": 0}},
                ],
            }
            reaction = {"reaction": {"content": "THUMBS_UP"}, "subject": subject}
            return {"data": {"addReaction": reaction}}
        if "removeReaction" in query:
            return {"data": {"removeReaction": {"reaction": {"content": "THUMBS_UP"}}}}
        raise AssertionError(f"unexpected GraphQL request: {query}")

    def _json_write(
        self,
        _root: str,
        *,
        host: list[str],
        token: str | None,
        method: str,
        endpoint: str,
        fields: dict[str, Any],
    ) -> tuple[int, str, str]:
        assert token == TOKEN
        self.json_calls.append((method, endpoint, fields))
        if endpoint == "graphql":
            is_write = "mutation" in fields["query"]
            if is_write and self.graphql_failure is not None:
                self._move_head(self.head_after_failure)
                return self.graphql_failure
            body = self._graphql(fields["query"], fields["variables"])
            if is_write and any(name in fields["query"] for name in self.errors_with_payload):
                body["errors"] = [{"message": "Field 'reactionGroups' failed", "path": ["x"]}]
                return 1, json.dumps(body), "gh: Field 'reactionGroups' failed"
            return 0, json.dumps(body), ""
        if self.rest_failure is not None:
            return self.rest_failure
        return 0, json.dumps({"id": 5, "body": fields.get("body", "")}), ""

    def _gh(
        self, argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        self.gh_calls.append((argv, token))
        assert TOKEN not in " ".join(argv), "the token must never reach argv"
        if "-X" in argv and self.rest_failure is not None:
            return self.rest_failure
        if argv[:2] == ["api", "--hostname"] and "-X" not in argv and argv[-1] in COMMENT_PATHS:
            return 0, json.dumps(self.comment), ""
        return 0, "", ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    return FakeGitHub(monkeypatch)


SESSION = "session_one"


def test_approve_refuses_a_moved_head_before_any_mutation(fake: FakeGitHub) -> None:
    fake.heads[7] = MOVED
    with pytest.raises(PullRequestChangedError) as refused:
        pr_actions.submit_review_verdict(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="APPROVE",
            body="Ship it",
            commit_oid=HEAD,
        )
    assert str(refused.value) == PULL_REQUEST_CHANGED_MESSAGE
    assert refused.value.reason == "pull_request_changed"
    assert fake.mutations("addPullRequestReview") == []


def test_approve_on_the_head_the_user_saw_is_submitted_with_its_commit(fake: FakeGitHub) -> None:
    result = pr_actions.submit_review_verdict(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        event="APPROVE",
        body="Ship it",
        commit_oid=HEAD,
    )
    assert result["event"] == "APPROVE"
    [sent] = fake.mutations("addPullRequestReview")
    assert sent["input"]["commitOID"] == HEAD
    assert sent["input"]["event"] == "APPROVE"


def test_request_changes_refuses_a_moved_head(fake: FakeGitHub) -> None:
    fake.heads[7] = MOVED
    with pytest.raises(PullRequestChangedError):
        pr_actions.submit_review_verdict(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="REQUEST_CHANGES",
            body="Fix this",
            commit_oid=HEAD,
        )
    assert fake.mutations("addPullRequestReview") == []


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
def test_the_comment_review_route_refuses_verdicts_before_the_host(
    fake: FakeGitHub, event: str
) -> None:
    with pytest.raises(InvalidPullRequestInput):
        pr_actions.submit_review(
            "/workspace", session_id=SESSION, pr_url=PR_URL, event=event, body="x", commit_oid=HEAD
        )
    assert fake.json_calls == []


def test_a_comment_review_is_one_review_on_the_head_the_user_saw(fake: FakeGitHub) -> None:
    result = pr_actions.submit_review(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        event="COMMENT",
        body="Nit",
        commit_oid=HEAD,
    )
    assert result["event"] == "COMMENT"
    [sent] = fake.mutations("addPullRequestReview")
    assert sent["input"] == {
        "pullRequestId": "PR_7",
        "commitOID": HEAD,
        "event": "COMMENT",
        "body": "Nit",
    }


def test_a_comment_review_refuses_a_moved_head(fake: FakeGitHub) -> None:
    fake.heads[7] = MOVED
    with pytest.raises(PullRequestChangedError):
        pr_actions.submit_review(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="COMMENT",
            body="Nit",
            commit_oid=HEAD,
        )
    assert fake.mutations("addPullRequestReview") == []


def test_submit_pending_refuses_a_moved_head(fake: FakeGitHub) -> None:
    fake.pending = [{"id": "R_1", "viewerDidAuthor": True, "commit": {"oid": HEAD}}]
    fake.heads[7] = MOVED
    with pytest.raises(PullRequestChangedError):
        pr_actions.submit_pending_review(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="COMMENT",
            body="",
            commit_oid=HEAD,
            pending_review_id="R_1",
        )
    assert fake.mutations("submitPullRequestReview") == []


def test_submit_pending_sends_the_pending_review(fake: FakeGitHub) -> None:
    fake.pending = [{"id": "R_1", "viewerDidAuthor": True, "commit": {"oid": HEAD}}]
    result = pr_actions.submit_pending_review(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        event="COMMENT",
        body="Done",
        commit_oid=HEAD,
        pending_review_id="R_1",
    )
    assert result["object"] == "session.github.pr_review"
    assert result["review"] == {
        "id": "R_1",
        "state": "COMMENTED",
        "body": "Done",
        "created_at": "2026-11-24T10:00:00Z",
        "url": f"{PR_URL}#pullrequestreview-1",
        "commit_oid": HEAD,
    }
    [sent] = fake.mutations("submitPullRequestReview")
    assert sent["input"]["pullRequestReviewId"] == "R_1"


@pytest.mark.parametrize("state", ["PENDING", "APPROVED", None])
def test_a_pending_review_submitted_in_another_state_is_an_unknown_outcome(
    fake: FakeGitHub, state: str | None
) -> None:
    """GitHub hasn't confirmed the review went out with the verdict the page asked for."""
    fake.pending = [{"id": "R_1", "viewerDidAuthor": True, "commit": {"oid": HEAD}}]
    review = {"id": "R_1", "state": state, "body": "Done", "commit": {"oid": HEAD}}
    answer = {"data": {"submitPullRequestReview": {"pullRequestReview": review}}}
    fake.graphql_failure = (0, json.dumps(answer), "")
    with pytest.raises(UnknownOutcomeError):
        pr_actions.submit_pending_review(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="COMMENT",
            body="Done",
            commit_oid=HEAD,
            pending_review_id="R_1",
        )


def test_submit_pending_without_a_pending_review_is_refused(fake: FakeGitHub) -> None:
    with pytest.raises(PullRequestError, match="no pending review"):
        pr_actions.submit_pending_review(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="COMMENT",
            body="",
            commit_oid=HEAD,
            pending_review_id="R_1",
        )


@pytest.mark.parametrize("kind", ["issue", "review"])
@pytest.mark.parametrize("action", ["edit", "delete", "react"])
def test_a_comment_from_another_pull_request_is_refused_before_any_write(
    fake: FakeGitHub, kind: str, action: str
) -> None:
    link_field = "issue_url" if kind == "issue" else "pull_request_url"
    fake.comment[link_field] = _comment_link(kind, 8)
    with pytest.raises(PullRequestError) as refused:
        if action == "edit":
            pr_actions.edit_comment(
                "/workspace",
                session_id=SESSION,
                pr_url=PR_URL,
                kind=kind,
                comment_id=5,
                body="Mine",
                expected_body_sha256=_digest("Original text"),
            )
        elif action == "delete":
            pr_actions.delete_comment(
                "/workspace", session_id=SESSION, pr_url=PR_URL, kind=kind, comment_id=5
            )
        else:
            pr_actions.add_reaction(
                "/workspace",
                session_id=SESSION,
                pr_url=PR_URL,
                kind=kind,
                comment_id=5,
                content="THUMBS_UP",
            )
    assert str(refused.value) == "This comment isn't on this pull request."
    assert fake.json_calls == []
    assert not [call for call in fake.gh_calls if "-X" in call[0]]
    assert fake.mutations("addReaction") == []


@pytest.mark.parametrize(
    ("pull_request_number", "repository"),
    [(8, "example-org/example-repo"), (7, "example-org/other-repo")],
)
def test_resolving_a_thread_from_another_pull_request_is_refused(
    fake: FakeGitHub, pull_request_number: int, repository: str
) -> None:
    fake.threads["T_OTHER"] = (pull_request_number, repository)
    with pytest.raises(PullRequestError):
        pr_actions.set_review_thread_resolved(
            "/workspace", session_id=SESSION, pr_url=PR_URL, thread_id="T_OTHER", resolved=True
        )
    assert fake.mutations("resolveReviewThread") == []


def test_a_graphql_refusal_after_the_head_moved_reports_pull_request_changed(
    fake: FakeGitHub,
) -> None:
    fake.refuse_mutation("addPullRequestReview", "Head sha didn't match", head_becomes=MOVED)
    with pytest.raises(PullRequestChangedError):
        pr_actions.submit_review_verdict(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="APPROVE",
            body="Ship it",
            commit_oid=HEAD,
        )
    assert len(fake.mutations("addPullRequestReview")) == 1


@pytest.mark.parametrize("message", ["expectedHeadOid is stale", "commitOID is not valid"])
def test_a_refusal_with_an_unmoved_head_is_the_ordinary_refusal(
    fake: FakeGitHub, message: str
) -> None:
    fake.refuse_mutation("addPullRequestReview", message)
    with pytest.raises(ValueError) as refused:
        pr_actions.submit_review_verdict(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="APPROVE",
            body="Ship it",
            commit_oid=HEAD,
        )
    assert str(refused.value) != PULL_REQUEST_CHANGED_MESSAGE
    assert getattr(refused.value, "reason", None) is None


def test_editing_a_comment_refuses_when_its_text_changed(fake: FakeGitHub) -> None:
    fake.comment["body"] = "Changed by someone else"
    with pytest.raises(CommentChangedError) as refused:
        pr_actions.edit_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            kind="issue",
            comment_id=5,
            body="Mine",
            expected_body_sha256=_digest("Original text"),
        )
    assert refused.value.reason == "comment_changed"
    assert _comment_writes(fake) == []


def test_editing_a_comment_sends_the_edit_when_the_digest_matches(fake: FakeGitHub) -> None:
    result = pr_actions.edit_comment(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        kind="issue",
        comment_id=5,
        body="Mine",
        expected_body_sha256=_digest("Original text"),
    )
    assert result["body"] == "Mine"
    assert _comment_writes(fake) == [("PATCH", COMMENT_PATH)]


def _comment_writes(fake: FakeGitHub) -> list[tuple[str, str]]:
    return [(method, endpoint) for method, endpoint, _ in fake.json_calls if method == "PATCH"]


def _change_comment(action: str, kind: str = "review") -> dict[str, Any]:
    if action == "edit":
        return pr_actions.edit_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            kind=kind,
            comment_id=5,
            body="Rewritten",
            expected_body_sha256=_digest("Original text"),
        )
    return pr_actions.delete_comment(
        "/workspace", session_id=SESSION, pr_url=PR_URL, kind=kind, comment_id=5
    )


def _rest_writes(fake: FakeGitHub) -> list[tuple[str, str]]:
    patches = _comment_writes(fake)
    deletes = [("DELETE", argv[-1]) for argv, _ in fake.gh_calls if "-X" in argv]
    return patches + deletes


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_someone_elses_comment_is_refused_with_nothing_sent(fake: FakeGitHub, action: str) -> None:
    """GitHub lets a repository admin change anyone's comment; here only the account's own."""
    fake.viewer_wrote_comment = False
    with pytest.raises(PullRequestError, match="Only your own comments"):
        _change_comment(action)
    assert _rest_writes(fake) == []


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_github_doesnt_say_who_wrote_is_refused(fake: FakeGitHub, action: str) -> None:
    del fake.comment["node_id"]
    with pytest.raises(PullRequestError, match="Only your own comments"):
        _change_comment(action)
    assert _rest_writes(fake) == []


@pytest.mark.parametrize("kind", ["issue", "review"])
def test_your_own_comment_is_deleted(fake: FakeGitHub, kind: str) -> None:
    result = _change_comment("delete", kind)
    assert result["object"] == "session.github.comment_deleted"
    path = COMMENT_PATH if kind == "issue" else REVIEW_COMMENT_PATH
    assert _rest_writes(fake) == [("DELETE", path)]


# After a write is sent, gh's stderr for failures GitHub may have applied anyway.
_UNCONFIRMED_STDERR = {
    "502": "gh: Bad Gateway (HTTP 502)",
    "reset": 'Post "https://api.github.com/graphql": read tcp: connection reset by peer',
    "eof": 'Post "https://api.github.com/graphql": unexpected EOF',
    # A repository or host named like a refusal's wording.
    "reset-sso-repository": (
        'Patch "https://api.github.com/repos/example-org/sso/pulls/comments/101": '
        "read tcp 10.0.0.2:51234->192.0.2.6:443: read: connection reset by peer"
    ),
    "reset-scope-repository": (
        'Post "https://api.github.com/repos/example-org/missing-scope/pulls/7": '
        "read: connection reset by peer"
    ),
    "eof-sso-host": 'Post "https://sso.example.com/api/graphql": unexpected EOF',
    "lookup-sso-host": "dial tcp: lookup sso.example.com: i/o timeout",
}
# And the ones that mean GitHub refused it without making it.
_REFUSED_STDERR = {
    "422": "gh: Validation Failed (HTTP 422)",
    "404": "gh: Not Found (HTTP 404)",
    "403": "gh: Resource not accessible by integration (HTTP 403)",
    "401": "gh: Bad credentials (HTTP 401)",
    "scope": "gh: INSUFFICIENT_SCOPES",
    # Its wording still counts with an address beside it.
    "sso": (
        "gh: Resource protected by organization SAML enforcement. See https://docs.github.com/sso"
    ),
}


def _resolve() -> dict[str, Any]:
    return pr_actions.set_review_thread_resolved(
        "/workspace", session_id=SESSION, pr_url=PR_URL, thread_id="T_1", resolved=True
    )


@pytest.mark.parametrize(
    "stderr", list(_UNCONFIRMED_STDERR.values()), ids=list(_UNCONFIRMED_STDERR)
)
def test_a_mutation_that_fails_after_the_send_is_an_unknown_outcome(
    fake: FakeGitHub, stderr: str
) -> None:
    fake.graphql_failure = (1, "", stderr)
    with pytest.raises(UnknownOutcomeError, match="Check the pull request before trying again"):
        _resolve()


@pytest.mark.parametrize(
    "stderr", list(_UNCONFIRMED_STDERR.values()), ids=list(_UNCONFIRMED_STDERR)
)
@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_change_that_fails_after_the_send_is_an_unknown_outcome(
    fake: FakeGitHub, action: str, stderr: str
) -> None:
    fake.rest_failure = (1, "", stderr)
    with pytest.raises(UnknownOutcomeError, match="Check the pull request before trying again"):
        _change_comment(action)


def test_a_5xx_with_graphql_errors_is_an_unknown_outcome_not_a_refusal(
    fake: FakeGitHub,
) -> None:
    """A 502 with an error body doesn't show GitHub refused the mutation."""
    answer = {"data": {"resolveReviewThread": None}, "errors": [{"message": "Not resolved"}]}
    fake.graphql_failure = (1, json.dumps(answer), "gh: Bad Gateway (HTTP 502)")
    with pytest.raises(UnknownOutcomeError):
        _resolve()
    assert len(fake.mutations("resolveReviewThread")) == 1


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_5xx_with_a_refusals_wording_is_an_unknown_outcome(
    fake: FakeGitHub, action: str
) -> None:
    fake.rest_failure = (1, "", "gh: Bad credentials or API rate limit exceeded (HTTP 503)")
    with pytest.raises(UnknownOutcomeError):
        _change_comment(action)


@pytest.mark.parametrize("stderr", list(_REFUSED_STDERR.values()), ids=list(_REFUSED_STDERR))
def test_a_write_github_definitely_refused_is_a_refusal(fake: FakeGitHub, stderr: str) -> None:
    fake.graphql_failure = (1, "", stderr)
    with pytest.raises(ValueError) as refused:
        _resolve()
    assert not isinstance(refused.value, UnknownOutcomeError)
    fake.graphql_failure = None
    fake.rest_failure = (1, "", stderr)
    with pytest.raises(ValueError) as refused:
        _change_comment("edit")
    assert not isinstance(refused.value, UnknownOutcomeError)


def test_graphql_errors_in_an_answer_that_parsed_are_a_refusal(fake: FakeGitHub) -> None:
    fake.refuse_mutation("resolveReviewThread", "Could not resolve to a node")
    with pytest.raises(ValueError) as refused:
        _resolve()
    assert not isinstance(refused.value, UnknownOutcomeError)


@pytest.mark.parametrize(
    "mutation", ["resolveReviewThread", "addReaction", "addPullRequestReview"]
)
def test_errors_beside_a_mutations_result_are_an_unknown_outcome_not_a_refusal(
    fake: FakeGitHub, mutation: str
) -> None:
    """GitHub made the change; a failed nested field mustn't make a client repeat it."""
    fake.errors_with_payload.add(mutation)
    with pytest.raises(UnknownOutcomeError):
        if mutation == "resolveReviewThread":
            _resolve()
        elif mutation == "addReaction":
            pr_actions.add_reaction(
                "/workspace",
                session_id=SESSION,
                pr_url=PR_URL,
                kind="issue",
                comment_id=5,
                content="THUMBS_UP",
            )
        else:
            pr_actions.submit_review_verdict(
                "/workspace",
                session_id=SESSION,
                pr_url=PR_URL,
                event="APPROVE",
                body="Ship it",
                commit_oid=HEAD,
            )
    assert len(fake.mutations(mutation)) == 1


@pytest.mark.parametrize("error_type", ["INTERNAL", "TIMEOUT", "SERVICE_UNAVAILABLE"])
def test_a_graphql_error_github_types_as_its_own_failure_is_an_unknown_outcome(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, error_type: str
) -> None:
    """Whatever the message says, GitHub failed on its side and may have made the change."""
    answer = {"data": {"resolveReviewThread": None}}
    answer["errors"] = [{"type": error_type, "message": "Could not resolve the thread"}]
    fake.graphql_failure = (1, json.dumps(answer), "gh: Could not resolve the thread")
    with pytest.raises(UnknownOutcomeError):
        _resolve()


def test_a_graphql_timeout_error_is_an_unknown_outcome(fake: FakeGitHub) -> None:
    fake.refuse_mutation("resolveReviewThread", "Something went wrong; this may be a timeout")
    with pytest.raises(UnknownOutcomeError):
        _resolve()


def test_an_edit_answered_with_an_error_body_on_exit_zero_is_an_unknown_outcome(
    fake: FakeGitHub,
) -> None:
    fake.rest_failure = (0, json.dumps({"message": "Server Error", "status": "500"}), "")
    with pytest.raises(UnknownOutcomeError):
        _change_comment("edit")


def test_an_unknown_outcome_is_never_reported_as_a_moved_head(fake: FakeGitHub) -> None:
    """The head moves while the verdict is sent; GitHub may still have recorded it."""
    fake.graphql_failure = (1, "", _UNCONFIRMED_STDERR["502"])
    fake.head_after_failure = MOVED
    with pytest.raises(UnknownOutcomeError):
        pr_actions.submit_review_verdict(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="APPROVE",
            body="Ship it",
            commit_oid=HEAD,
        )
    assert fake.heads[7] == MOVED


def test_a_refusal_after_the_head_moved_during_the_send_is_a_moved_head(
    fake: FakeGitHub,
) -> None:
    fake.graphql_failure = (1, "", "gh: Validation Failed (HTTP 422)")
    fake.head_after_failure = MOVED
    with pytest.raises(PullRequestChangedError):
        pr_actions.submit_review_verdict(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            event="APPROVE",
            body="Ship it",
            commit_oid=HEAD,
        )


def _token_lookup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token lookup at the action's start runs out of its deadline, before any read."""

    def timed_out(_root: str, _reference: PullRequestRef) -> str | None:
        if github._gh_deadline.get() is None:
            return TOKEN
        # Inside the write's deadline scope, which resets this when it ends.
        github._gh_timed_out.set(True)
        return None

    monkeypatch.setattr(github, "_pr_token", timed_out)


def test_a_mutation_whose_token_lookup_timed_out_is_never_sent(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sending it anyway would run it as gh's default account, not the selected one."""
    _token_lookup_times_out(monkeypatch)
    with pytest.raises(ValueError, match="Nothing was sent") as refused:
        _resolve()
    assert not isinstance(refused.value, UnknownOutcomeError)
    assert fake.mutations("resolveReviewThread") == []


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_change_whose_token_lookup_timed_out_is_never_sent(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _token_lookup_times_out(monkeypatch)
    with pytest.raises(ValueError, match="Nothing was sent"):
        _change_comment(action)
    assert _rest_writes(fake) == []


def test_a_reaction_is_added_to_the_comments_node(fake: FakeGitHub) -> None:
    result = pr_actions.add_reaction(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        kind="issue",
        comment_id=5,
        content="THUMBS_UP",
    )
    assert result["reacted"] is True
    [sent] = fake.mutations("addReaction")
    assert sent["input"] == {"subjectId": "IC_1", "content": "THUMBS_UP"}
    # The comment's reactions as GitHub has them after the change.
    assert result["reactions"] == [{"content": "THUMBS_UP", "count": 2, "viewer_reacted": True}]
    assert result["can_react"] is True


def _answer(mutation: str, payload: dict[str, Any]) -> tuple[int, str, str]:
    """A sent mutation's answer, as ``gh`` prints it on success."""
    return 0, json.dumps({"data": {mutation: payload}}), ""


def _group(content: str, reacted: bool) -> dict[str, Any]:
    return {"content": content, "viewerHasReacted": reacted, "reactors": {"totalCount": 1}}


@pytest.mark.parametrize(
    ("verb", "payload"),
    [
        ("add", {"reaction": {"content": "HEART"}}),
        ("add", {"subject": {"reactionGroups": [_group("THUMBS_UP", False)]}}),
        ("remove", {"subject": {"reactionGroups": [_group("THUMBS_UP", True)]}}),
        ("add", {}),
        ("remove", {}),
        ("add", {"reaction": None, "subject": None}),
        ("remove", {"reaction": None, "subject": None}),
        ("add", {"subject": {"reactionGroups": [_group("HEART", True)]}}),
        ("remove", {"subject": {"reactionGroups": []}}),
        ("add", {"subject": {"reactionGroups": [{"content": "THUMBS_UP"}]}}),
    ],
    ids=[
        "add-another-reaction",
        "add-not-reacted",
        "remove-still-reacted",
        "add-empty",
        "remove-empty",
        "add-null-fields",
        "remove-null-fields",
        "add-subject-without-its-group",
        "remove-subject-without-groups",
        "add-group-without-viewer-state",
    ],
)
def test_a_reaction_change_github_does_not_confirm_is_an_unknown_outcome(
    fake: FakeGitHub, verb: str, payload: dict[str, Any]
) -> None:
    mutation = f"{verb}Reaction"
    fake.graphql_failure = _answer(mutation, payload)
    change = pr_actions.add_reaction if verb == "add" else pr_actions.remove_reaction
    with pytest.raises(UnknownOutcomeError):
        change(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            kind="issue",
            comment_id=5,
            content="THUMBS_UP",
        )
    assert len(fake.mutations(mutation)) == 1


@pytest.mark.parametrize("verb", ["add", "remove"])
def test_a_reaction_change_confirmed_only_by_its_group_succeeds(
    fake: FakeGitHub, verb: str
) -> None:
    mutation = f"{verb}Reaction"
    group = _group("THUMBS_UP", verb == "add")
    fake.graphql_failure = _answer(mutation, {"subject": {"reactionGroups": [group]}})
    change = pr_actions.add_reaction if verb == "add" else pr_actions.remove_reaction
    result = change(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        kind="issue",
        comment_id=5,
        content="THUMBS_UP",
    )
    assert result["reacted"] is (verb == "add")
    assert len(fake.mutations(mutation)) == 1


def test_a_reaction_change_github_answers_without_the_comment_says_none(
    fake: FakeGitHub,
) -> None:
    result = pr_actions.remove_reaction(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        kind="issue",
        comment_id=5,
        content="THUMBS_UP",
    )
    assert result["reacted"] is False
    assert "reactions" not in result and "can_react" not in result


def test_a_reaction_must_be_a_github_reaction(fake: FakeGitHub) -> None:
    with pytest.raises(InvalidPullRequestInput):
        pr_actions.add_reaction(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            kind="issue",
            comment_id=5,
            content="meh",
        )


@pytest.mark.parametrize(
    ("resolved", "mutation", "other"),
    [
        # A leading space, since "resolveReviewThread" is inside "unresolveReviewThread".
        (True, " resolveReviewThread(", "unresolveReviewThread("),
        (False, "unresolveReviewThread(", " resolveReviewThread("),
    ],
    ids=["resolve", "reopen"],
)
def test_resolving_a_thread_names_the_thread(
    fake: FakeGitHub, resolved: bool, mutation: str, other: str
) -> None:
    result = pr_actions.set_review_thread_resolved(
        "/workspace", session_id=SESSION, pr_url=PR_URL, thread_id="T_1", resolved=resolved
    )
    assert result["resolved"] is resolved
    assert fake.mutations(mutation) == [{"input": {"threadId": "T_1"}}]
    assert fake.mutations(other) == []


@pytest.mark.parametrize(
    "thread",
    [None, {"id": "T_other", "isResolved": True}, {"id": "T_1", "isResolved": False}],
    ids=["no-thread", "another-thread", "still-open"],
)
def test_a_resolve_github_did_not_confirm_is_an_unknown_outcome(
    fake: FakeGitHub, thread: dict[str, Any] | None
) -> None:
    answer = {"data": {"resolveReviewThread": {"thread": thread}}}
    fake.graphql_failure = (0, json.dumps(answer), "")
    with pytest.raises(UnknownOutcomeError):
        _resolve()


def test_a_non_github_pull_request_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    gitlab = PullRequestRef(
        provider="gitlab",
        host="gitlab.com",
        repository="example-org/example-repo",
        number=3,
        url="https://gitlab.com/example-org/example-repo/-/merge_requests/3",
    )
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda _session, _url: gitlab)
    with pytest.raises(ValueError, match="supported only for GitHub"):
        pr_actions.submit_review(
            "/workspace",
            session_id=SESSION,
            pr_url=gitlab.url,
            event="COMMENT",
            body="x",
            commit_oid=HEAD,
        )


def _chosen_login_signed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login is chosen for the repository, but the action's token lookup at its start fails."""
    monkeypatch.setattr(
        github,
        "_pr_token",
        lambda _root, _reference: TOKEN if github._gh_deadline.get() is None else None,
    )
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repository: "octocat")


def test_a_login_cleared_during_the_token_lookup_still_refuses_the_mutation(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another request clearing the saved login mid-write must not let this one run as another."""
    _chosen_login_signed_out(monkeypatch)
    cleared = False
    monkeypatch.setattr(
        github._config,
        "github_account_preference",
        lambda _repository: None if cleared else "octocat",
    )

    def lookup_cleared_mid_write(_root: str, _reference: PullRequestRef) -> str | None:
        nonlocal cleared
        if github._gh_deadline.get() is None:
            return TOKEN
        cleared = True  # Another request clears the login while the write's lookup runs.
        return None

    monkeypatch.setattr(github, "_pr_token", lookup_cleared_mid_write)
    with pytest.raises(ValueError, match="octocat isn't signed in on the host"):
        _resolve()
    assert fake.mutations("resolveReviewThread") == []


def _lookups_answer(monkeypatch: pytest.MonkeyPatch, *tokens: str | None) -> list[str]:
    """octocat is chosen, and each ``gh auth token`` lookup answers the next of ``tokens``."""
    lookups: list[str] = []
    monkeypatch.setattr(github, "_pr_token", _REAL_PR_TOKEN)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repository: "octocat")

    def lookup(_root: str, login: str, _host: str = "github.com") -> str | None:
        lookups.append(login)
        return tokens[min(len(lookups), len(tokens)) - 1]

    monkeypatch.setattr(github, "_gh_auth_token", lookup)
    return lookups


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_change_whose_login_signs_in_midway_reads_nothing_as_another_account(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The authorship check must not run as the active account while the write runs as octocat."""
    lookups = _lookups_answer(monkeypatch, None, TOKEN)
    with pytest.raises(ValueError, match="octocat isn't signed in on the host"):
        _change_comment(action)
    assert lookups == ["octocat"]
    assert fake.gh_calls == [] and fake.json_calls == []


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_changes_checks_and_write_run_with_the_one_token_looked_up(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A later lookup answering another account changes nothing: there's only the first."""
    lookups = _lookups_answer(monkeypatch, TOKEN, "ghp_other_account")
    _change_comment(action)
    assert lookups == ["octocat"]
    assert fake.json_calls and {token for _argv, token in fake.gh_calls} == {TOKEN}


def test_a_pending_review_read_for_a_signed_out_chosen_login_is_refused(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drafts are the account's own; the active account's must not stand in for them."""
    fake.pending = _own_pending(_DRAFT)
    _lookups_answer(monkeypatch, None)
    with pytest.raises(ValueError, match="octocat isn't signed in on the host"):
        pr_actions.get_pending_review("/workspace", session_id=SESSION, pr_url=PR_URL)
    assert fake.json_calls == []


def test_a_mutation_for_a_signed_out_chosen_login_is_never_sent(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sending it would run it as the host's active account instead of the chosen one."""
    _chosen_login_signed_out(monkeypatch)
    with pytest.raises(ValueError, match="octocat isn't signed in on the host") as refused:
        _resolve()
    assert not isinstance(refused.value, UnknownOutcomeError)
    assert fake.mutations("resolveReviewThread") == []


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_change_for_a_signed_out_chosen_login_is_never_sent(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _chosen_login_signed_out(monkeypatch)
    with pytest.raises(ValueError, match="octocat isn't signed in on the host"):
        _change_comment(action)
    assert _rest_writes(fake) == []


@pytest.mark.parametrize("chosen", [None, "octocat"])
def test_a_write_without_a_token_runs_only_in_a_sandbox(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, chosen: str | None
) -> None:
    """A sandbox has only its own identity, so gh's own account is the one meant."""
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: None)
    monkeypatch.setattr(github, "_in_sandbox", lambda: True)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repository: chosen)
    monkeypatch.setattr(github, "_gh_active_token", _never_looked_up, raising=False)
    tokens: list[str | None] = []

    def json_write(root: str, **kwargs: Any) -> tuple[int, str, str]:
        tokens.append(kwargs["token"])
        return fake._json_write(root, **{**kwargs, "token": TOKEN})

    monkeypatch.setattr(github, "_pr_json_write", json_write)
    _resolve()
    assert len(fake.mutations("resolveReviewThread")) == 1
    assert tokens and set(tokens) == {None}


def _never_looked_up(*_args: Any, **_kwargs: Any) -> str | None:
    raise AssertionError("a sandbox has one identity; there is no active account to pin")


_OTHER_TOKEN = "ghp_other_account_token"


def _no_login_chosen(monkeypatch: pytest.MonkeyPatch, *active: str | None) -> list[str]:
    """A local host with no login chosen; each active-account lookup answers the next token."""
    lookups: list[str] = []
    monkeypatch.setattr(github, "_pr_token", _REAL_PR_TOKEN)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repository: None)

    def lookup(_root: str, host: str = "github.com") -> str | None:
        lookups.append(host)
        return active[min(len(lookups), len(active)) - 1]

    monkeypatch.setattr(github, "_gh_active_token", lookup, raising=False)
    return lookups


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_a_comment_change_with_no_login_chosen_stays_the_account_active_when_it_began(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Switching gh's active account after the authorship check must not change who writes."""
    # The first lookup answers the account active at the start; any later one, another.
    lookups = _no_login_chosen(monkeypatch, TOKEN, _OTHER_TOKEN)

    _change_comment(action)

    assert lookups == ["github.com"]
    # The fake answers a JSON write only with TOKEN; a delete is a plain gh call.
    assert len(_rest_writes(fake)) == 1
    # The comment read and authorship check before it ran as that account too.
    assert {token for _argv, token in fake.gh_calls} == {TOKEN}


def test_a_mutation_with_no_login_chosen_stays_the_account_active_when_it_began(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups = _no_login_chosen(monkeypatch, TOKEN, _OTHER_TOKEN)

    _resolve()

    # The fake GraphQL endpoint refuses any token but TOKEN.
    assert lookups == ["github.com"]
    assert len(fake.mutations("resolveReviewThread")) == 1


_COMMON = {"session_id": SESSION, "pr_url": PR_URL}
_REACTION = {**_COMMON, "kind": "issue", "comment_id": 5, "content": "THUMBS_UP"}
# Every review action, as the page sends it.
_REVIEW_ACTIONS: dict[str, Callable[[], object]] = {
    "edit": lambda: _change_comment("edit"),
    "delete": lambda: _change_comment("delete"),
    "resolve": _resolve,
    "read-pending": lambda: pr_actions.get_pending_review("/workspace", **_COMMON),
    "start-pending": lambda: pr_actions.start_pending_review(
        "/workspace", commit_oid=HEAD, **_COMMON
    ),
    **{
        f"pending-{action}": lambda action=action: _pending_change(action, "R_1")
        for action in ["add", "reply", "update", "delete", "submit", "discard"]
    },
    "comment-review": lambda: pr_actions.submit_review(
        "/workspace", event="COMMENT", body="Looks fine", commit_oid=HEAD, **_COMMON
    ),
    "approve": lambda: pr_actions.submit_review_verdict(
        "/workspace", event="APPROVE", body="Ship it", commit_oid=HEAD, **_COMMON
    ),
    "request-changes": lambda: pr_actions.submit_review_verdict(
        "/workspace", event="REQUEST_CHANGES", body="Not yet", commit_oid=HEAD, **_COMMON
    ),
    "add-reaction": lambda: pr_actions.add_reaction("/workspace", **_REACTION),
    "remove-reaction": lambda: pr_actions.remove_reaction("/workspace", **_REACTION),
}


@pytest.mark.parametrize("action", sorted(_REVIEW_ACTIONS))
def test_a_change_whose_active_account_cant_be_read_is_never_sent(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Without the active account's token, each call could run as a different account."""
    fake.pending = _own_pending(_DRAFT)
    _no_login_chosen(monkeypatch, None, TOKEN)

    with pytest.raises(ValueError, match="Nothing was sent") as refused:
        _REVIEW_ACTIONS[action]()

    assert not isinstance(refused.value, UnknownOutcomeError)
    # Nothing was read or written.
    assert fake.gh_calls == [] and fake.json_calls == []


_ENTERPRISE_PR = PullRequestRef.from_url("https://ghe.example.com/example-org/example-repo/pull/7")


def _pull_request_on(host: str, repository: str) -> PullRequestRef:
    return PullRequestRef(
        provider="github",
        host=host,
        repository=repository,
        number=7,
        url=f"https://{host}/{repository}/pull/7",
    )


@pytest.mark.parametrize(
    ("host", "repository", "stderr"),
    [
        ("sso", "example-org/example-repo", "error connecting to sso"),
        ("sso", "example-org/example-repo", "dial tcp: lookup sso: i/o timeout"),
        ("github.com", "sso/example-repo", "talking to sso: connection reset by peer"),
        ("github.com", "example-org/missing-scope", "missing-scope: unexpected EOF"),
    ],
    ids=["dotless-host", "dotless-host-lookup", "owner", "repository"],
)
def test_a_failure_naming_the_pull_requests_own_host_or_repository_is_an_unknown_outcome(
    host: str, repository: str, stderr: str
) -> None:
    """A dotless Enterprise host, an owner or a repository named like a refusal's wording."""
    reference = _pull_request_on(host, repository)
    failure = github_failures.write_failure(stderr, reference=reference, action="x")
    assert isinstance(failure, UnknownOutcomeError)


@pytest.mark.parametrize(
    "stderr",
    ["gh: Not Found (HTTP 404)", "gh: Resource protected by organization SAML enforcement."],
)
def test_a_refusal_from_a_pull_request_named_like_one_is_still_a_refusal(stderr: str) -> None:
    reference = _pull_request_on("sso", "sso/missing-scope")
    failure = github_failures.write_failure(stderr, reference=reference, action="x")
    assert not isinstance(failure, UnknownOutcomeError)


def test_failure_classes_name_the_host_and_never_the_token() -> None:
    # Each input carries the token, as a verbose gh might print it.
    cases = {
        "needs the repo scope": f"INSUFFICIENT_SCOPES token={TOKEN}",
        "sso": f"Resource protected by organization SAML enforcement {TOKEN}",
        "rate": f"API rate limit exceeded for {TOKEN}",
        "signed out": f"Bad token {TOKEN}. To get started, please run: gh auth login",
    }
    messages = {
        key: str(github_failures.refusal(stderr, reference=_ENTERPRISE_PR, action="x"))
        for key, stderr in cases.items()
    }
    assert "needs the `repo` scope" in messages["needs the repo scope"]
    assert messages["sso"] == "Authorize the login on ghe.example.com for SSO."
    assert "rate limiting" in messages["rate"]
    assert "gh auth login" in messages["signed out"]
    assert all(TOKEN not in message for message in messages.values())


_DRAFT = {"id": "PRRC_draft", "body": "Draft text"}
_DRAFT_SHA = hashlib.sha256(b"Draft text").hexdigest()


def _own_pending(*drafts: dict[str, Any], viewer: bool = True) -> list[dict[str, Any]]:
    return [
        {
            "id": "R_1",
            "viewerDidAuthor": viewer,
            "commit": {"oid": HEAD},
            "comments": {"nodes": list(drafts)},
        }
    ]


def _draft_writes(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [
        *fake.mutations("updatePullRequestReviewComment"),
        *fake.mutations("deletePullRequestReviewComment"),
        *fake.mutations("addPullRequestReviewThreadReply"),
    ]


def test_a_draft_in_the_viewers_pending_review_is_updated_while_its_text_is_as_read(
    fake: FakeGitHub,
) -> None:
    fake.pending = _own_pending(_DRAFT)
    result = pr_actions.update_pending_review_comment(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        comment_node_id="PRRC_draft",
        pending_review_id="R_1",
        body="New text",
        expected_body_sha256=_DRAFT_SHA,
    )
    assert result == {
        "object": "session.github.pr_comment_edit",
        "pr_url": PR_URL,
        "comment_id": "PRRC_draft",
        "body": "New text",
    }
    [sent] = fake.mutations("updatePullRequestReviewComment")
    assert sent["input"] == {"pullRequestReviewCommentId": "PRRC_draft", "body": "New text"}


def test_a_stale_draft_digest_is_refused_with_nothing_sent(fake: FakeGitHub) -> None:
    fake.pending = _own_pending({**_DRAFT, "body": "Changed elsewhere"})
    with pytest.raises(CommentChangedError):
        pr_actions.update_pending_review_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            comment_node_id="PRRC_draft",
            pending_review_id="R_1",
            body="New text",
            expected_body_sha256=_DRAFT_SHA,
        )
    assert _draft_writes(fake) == []


@pytest.mark.parametrize(
    "pending",
    [
        pytest.param([], id="no-pending-review"),
        pytest.param(_own_pending({"id": "PRRC_other", "body": "x"}), id="not-in-the-review"),
        pytest.param(_own_pending(_DRAFT, viewer=False), id="someone-elses-review"),
    ],
)
@pytest.mark.parametrize("action", ["update", "delete"])
def test_a_draft_outside_the_viewers_pending_review_is_refused_with_nothing_sent(
    fake: FakeGitHub, pending: list[dict[str, Any]], action: str
) -> None:
    fake.pending = pending
    with pytest.raises(PullRequestError, match="isn't in your pending review"):
        if action == "update":
            pr_actions.update_pending_review_comment(
                "/workspace",
                session_id=SESSION,
                pr_url=PR_URL,
                comment_node_id="PRRC_draft",
                pending_review_id="R_1",
                body="New text",
                expected_body_sha256=_DRAFT_SHA,
            )
        else:
            pr_actions.delete_pending_review_comment(
                "/workspace",
                session_id=SESSION,
                pr_url=PR_URL,
                comment_node_id="PRRC_draft",
                pending_review_id="R_1",
            )
    assert _draft_writes(fake) == []


def test_a_draft_in_the_viewers_pending_review_is_deleted(fake: FakeGitHub) -> None:
    fake.pending = _own_pending(_DRAFT)
    result = pr_actions.delete_pending_review_comment(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        comment_node_id="PRRC_draft",
        pending_review_id="R_1",
    )
    assert result == {
        "object": "session.github.pr_comment_delete",
        "pr_url": PR_URL,
        "comment_id": "PRRC_draft",
    }
    [sent] = fake.mutations("deletePullRequestReviewComment")
    assert sent["input"] == {"id": "PRRC_draft"}


@pytest.mark.parametrize(
    ("review", "confirmed"),
    [({"id": "R_other"}, False), ("R_1", False), (None, True)],
    ids=["another-review", "not-a-review", "no-review"],
)
def test_a_draft_deletion_naming_another_review_is_an_unknown_outcome(
    fake: FakeGitHub, review: Any, confirmed: bool
) -> None:
    fake.pending = _own_pending(_DRAFT)
    answer = {"pullRequestReview": review}
    fake.graphql_failure = _answer("deletePullRequestReviewComment", answer)

    def delete() -> dict[str, Any]:
        return pr_actions.delete_pending_review_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            comment_node_id="PRRC_draft",
            pending_review_id="R_1",
        )

    if confirmed:
        assert delete()["comment_id"] == "PRRC_draft"
    else:
        with pytest.raises(UnknownOutcomeError):
            delete()
    assert len(fake.mutations("deletePullRequestReviewComment")) == 1


def test_a_draft_reply_joins_the_viewers_pending_review_in_a_conversation_on_this_pr(
    fake: FakeGitHub,
) -> None:
    fake.pending = _own_pending()
    result = pr_actions.add_pending_review_comment(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        body="Agreed",
        anchor=None,
        in_reply_to_thread_id="T_1",
        pending_review_id="R_1",
        expected_head=HEAD,
    )
    assert result["object"] == "session.github.pr_review_draft"
    assert (result["review_id"], result["thread_id"]) == ("R_1", "T_1")
    assert result["comment"]["id"] == "PRRC_new"
    assert result["comment"]["body"] == "Agreed"
    assert result["comment"]["pending"] is True
    [sent] = fake.mutations("addPullRequestReviewThreadReply")
    assert sent["input"] == {
        "pullRequestReviewId": "R_1",
        "pullRequestReviewThreadId": "T_1",
        "body": "Agreed",
    }


@pytest.mark.parametrize(
    ("threads", "pending", "match"),
    [
        pytest.param(
            {"T_1": (8, "example-org/example-repo")},
            None,
            "isn't on this pull request",
            id="other-pr",
        ),
        pytest.param({}, None, "isn't on this pull request", id="unknown-thread"),
        pytest.param(None, [], "no pending review", id="no-pending-review"),
    ],
)
def test_a_draft_reply_is_refused_with_nothing_sent(
    fake: FakeGitHub,
    threads: dict[str, tuple[int, str]] | None,
    pending: list[dict[str, Any]] | None,
    match: str,
) -> None:
    fake.pending = _own_pending() if pending is None else pending
    if threads is not None:
        fake.threads = threads
    with pytest.raises(PullRequestError, match=match):
        pr_actions.add_pending_review_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            body="Agreed",
            anchor=None,
            in_reply_to_thread_id="T_1",
            pending_review_id="R_1",
            expected_head=HEAD,
        )
    assert _draft_writes(fake) == []
    # Nothing starts a review for a reply, either.
    assert fake.mutations("addPullRequestReview(") == []


def test_a_reply_with_lines_is_refused(fake: FakeGitHub) -> None:
    anchor = {"path": "a.py", "side": "RIGHT", "line": 1, "head_sha": HEAD, "base_sha": BASE}
    with pytest.raises(InvalidPullRequestInput):
        pr_actions.add_pending_review_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            body="Agreed",
            anchor=anchor,
            in_reply_to_thread_id="T_1",
            pending_review_id="R_1",
            expected_head=HEAD,
        )
    assert _draft_writes(fake) == []


def test_a_draft_past_the_first_hundred_is_found_on_a_later_page(fake: FakeGitHub) -> None:
    others = [{"id": f"PRRC_other_{index}", "body": "x"} for index in range(250)]
    fake.pending = _own_pending(*others, _DRAFT)
    pr_actions.delete_pending_review_comment(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        comment_node_id="PRRC_draft",
        pending_review_id="R_1",
    )
    [sent] = fake.mutations("deletePullRequestReviewComment")
    assert sent["input"] == {"id": "PRRC_draft"}
    # Three pages were read: 100, 100, then the last 51.
    assert len(fake.mutations("on PullRequestReview {")) == 3


def _pending_change(action: str, review_id: str) -> None:
    """Send ``action`` to the pending review named ``review_id``, as the page would."""
    common = {"session_id": SESSION, "pr_url": PR_URL, "pending_review_id": review_id}
    anchor = {"path": "a.py", "side": "RIGHT", "line": 1, "head_sha": HEAD, "base_sha": BASE}
    if action == "add":
        pr_actions.add_pending_review_comment(
            "/workspace", body="New", anchor=anchor, expected_head=HEAD, **common
        )
    elif action == "reply":
        pr_actions.add_pending_review_comment(
            "/workspace",
            body="New",
            anchor=None,
            in_reply_to_thread_id="T_1",
            expected_head=HEAD,
            **common,
        )
    elif action == "update":
        pr_actions.update_pending_review_comment(
            "/workspace",
            comment_node_id="PRRC_draft",
            body="New",
            expected_body_sha256=_DRAFT_SHA,
            **common,
        )
    elif action == "delete":
        pr_actions.delete_pending_review_comment(
            "/workspace", comment_node_id="PRRC_draft", **common
        )
    elif action == "submit":
        pr_actions.submit_pending_review(
            "/workspace", event="COMMENT", body="", commit_oid=HEAD, **common
        )
    else:
        pr_actions.discard_pending_review("/workspace", **common)


_PENDING_ACTIONS = ["add", "reply", "update", "delete", "submit", "discard"]


def _pending_writes(fake: FakeGitHub) -> list[dict[str, Any]]:
    names = (
        "addPullRequestReviewThread",
        "updatePullRequestReviewComment",
        "deletePullRequestReviewComment",
        "submitPullRequestReview",
        "deletePullRequestReview(",
    )
    return [write for name in names for write in fake.mutations(name)]


@pytest.mark.parametrize("action", _PENDING_ACTIONS)
def test_a_change_to_another_pending_review_than_the_page_saw_is_refused(
    fake: FakeGitHub, action: str
) -> None:
    """Submitted and replaced elsewhere: the viewer's pending review is now another one."""
    fake.pending = _own_pending(_DRAFT)
    with pytest.raises(PullRequestError, match=r"changed since this page read it|isn.t in your"):
        _pending_change(action, "R_old")
    assert _pending_writes(fake) == []


@pytest.mark.parametrize("action", _PENDING_ACTIONS)
def test_a_change_without_the_pending_review_id_is_refused_before_any_read(
    fake: FakeGitHub, action: str
) -> None:
    fake.pending = _own_pending(_DRAFT)
    with pytest.raises(InvalidPullRequestInput, match="pending review the page showed"):
        _pending_change(action, "")
    assert fake.json_calls == []


@pytest.mark.parametrize("action", ["add", "reply"])
def test_a_draft_add_refuses_a_moved_head_with_nothing_sent(fake: FakeGitHub, action: str) -> None:
    fake.pending = _own_pending()
    fake.heads[7] = MOVED
    with pytest.raises(PullRequestChangedError):
        _pending_change(action, "R_1")
    assert _pending_writes(fake) == []


def test_a_line_draft_answers_its_new_conversation_in_the_pages_shape(fake: FakeGitHub) -> None:
    fake.pending = _own_pending()
    result = pr_actions.add_pending_review_comment(
        "/workspace",
        session_id=SESSION,
        pr_url=PR_URL,
        body="Why strip here?",
        anchor={
            "path": "src/parser.py",
            "side": "RIGHT",
            "line": 4,
            "start_line": 2,
            "head_sha": HEAD,
            "base_sha": BASE,
        },
        pending_review_id="R_1",
        expected_head=HEAD,
    )
    assert result["object"] == "session.github.pr_review_draft"
    assert result["review_id"] == "R_1"
    thread = result["thread"]
    assert (thread["id"], thread["path"], thread["side"]) == ("T_new", "src/parser.py", "RIGHT")
    assert (thread["line"], thread["start_line"]) == (4, 2)
    [draft] = thread["comments"]["data"]
    assert (draft["id"], draft["body"], draft["pending"]) == ("PRRC_line", "Why strip here?", True)
    [sent] = fake.mutations("addPullRequestReviewThread(")
    assert sent["input"]["pullRequestReviewId"] == "R_1"
    assert (sent["input"]["startLine"], sent["input"]["startSide"]) == (2, "RIGHT")
    # A draft joins the review the page saw; it never starts one itself.
    assert fake.mutations("addPullRequestReview(") == []


def test_a_line_draft_needs_its_lines_picked_on_the_expected_head(fake: FakeGitHub) -> None:
    fake.pending = _own_pending()
    anchor = {"path": "a.py", "side": "RIGHT", "line": 1, "head_sha": MOVED, "base_sha": BASE}
    with pytest.raises(InvalidPullRequestInput):
        pr_actions.add_pending_review_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            body="New",
            anchor=anchor,
            pending_review_id="R_1",
            expected_head=HEAD,
        )
    assert fake.json_calls == []


def test_a_line_draft_refuses_a_moved_base_with_nothing_sent(fake: FakeGitHub) -> None:
    """Lines picked against another base would land on a different diff."""
    fake.pending = _own_pending()
    anchor = {"path": "a.py", "side": "RIGHT", "line": 1, "head_sha": HEAD, "base_sha": MOVED}
    with pytest.raises(PullRequestChangedError):
        pr_actions.add_pending_review_comment(
            "/workspace",
            session_id=SESSION,
            pr_url=PR_URL,
            body="New",
            anchor=anchor,
            pending_review_id="R_1",
            expected_head=HEAD,
        )
    assert fake.mutations("addPullRequestReviewThread(") == []


def test_a_discard_names_the_review_it_deleted(fake: FakeGitHub) -> None:
    fake.pending = _own_pending()
    result = pr_actions.discard_pending_review(
        "/workspace", session_id=SESSION, pr_url=PR_URL, pending_review_id="R_1"
    )
    assert result == {
        "object": "session.github.pr_review_discard",
        "pr_url": PR_URL,
        "review_id": "R_1",
    }


@pytest.mark.parametrize(
    ("review", "confirmed"),
    [({"id": "R_other"}, False), (None, True)],
    ids=["another-review", "no-review"],
)
def test_a_discard_naming_another_review_is_an_unknown_outcome(
    fake: FakeGitHub, review: Any, confirmed: bool
) -> None:
    fake.pending = _own_pending()
    answer = {"pullRequestReview": review}
    fake.graphql_failure = _answer("deletePullRequestReview", answer)

    def discard() -> dict[str, Any]:
        return pr_actions.discard_pending_review(
            "/workspace", session_id=SESSION, pr_url=PR_URL, pending_review_id="R_1"
        )

    if confirmed:
        assert discard()["review_id"] == "R_1"
    else:
        with pytest.raises(UnknownOutcomeError):
            discard()
    assert len(fake.mutations("deletePullRequestReview")) == 1


def test_a_started_review_answers_only_itself(fake: FakeGitHub) -> None:
    """Starting a review reads back nothing more than what the mutation answered."""
    result = pr_actions.start_pending_review(
        "/workspace", session_id=SESSION, pr_url=PR_URL, commit_oid=HEAD
    )
    assert result == {
        "object": "session.github.pr_review_start",
        "pr_url": PR_URL,
        "review_id": "R_1",
        "commit_oid": HEAD,
    }
    assert fake.mutations("on PullRequestReview {") == []
    assert fake.mutations("reviewThreads(") == []


@pytest.mark.parametrize(
    "review",
    [
        {"id": "R_1", "state": "COMMENTED", "viewerDidAuthor": True, "commit": {"oid": HEAD}},
        {"id": "R_1", "state": "PENDING", "viewerDidAuthor": True, "commit": {"oid": MOVED}},
        {"id": "R_1", "state": "PENDING", "viewerDidAuthor": False, "commit": {"oid": HEAD}},
    ],
    ids=["submitted", "another-commit", "someone-elses"],
)
def test_a_start_github_did_not_confirm_is_an_unknown_outcome(
    fake: FakeGitHub, review: dict[str, Any]
) -> None:
    fake.graphql_failure = _answer("addPullRequestReview", {"pullRequestReview": review})
    with pytest.raises(UnknownOutcomeError):
        pr_actions.start_pending_review(
            "/workspace", session_id=SESSION, pr_url=PR_URL, commit_oid=HEAD
        )
    assert len(fake.mutations("addPullRequestReview(")) == 1


def test_a_start_refuses_a_moved_head_with_nothing_sent(fake: FakeGitHub) -> None:
    fake.heads[7] = MOVED
    with pytest.raises(PullRequestChangedError):
        pr_actions.start_pending_review(
            "/workspace", session_id=SESSION, pr_url=PR_URL, commit_oid=HEAD
        )
    assert fake.mutations("addPullRequestReview(") == []
