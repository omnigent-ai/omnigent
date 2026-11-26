"""Closing, reopening, draft changes and auto-merge, against a fake ``gh`` and GraphQL endpoint.

A change checks the pull request's state first and refuses what wouldn't change. It runs as
the pull request's account, and reports a failure after the send as refused only when
GitHub definitely refused it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner import pr_actions, pr_resource
from omnigent.runner.pull_request_errors import (
    PULL_REQUEST_CHANGED_MESSAGE,
    UNKNOWN_OUTCOME_MESSAGE,
    InvalidPullRequestInput,
    UnknownOutcomeError,
)
from omnigent.runner.session_prs import PullRequestRef

TOKEN = "ghp_example_token_value"
OTHER_TOKEN = "ghp_other_account_token"
# The token lookup FakeGitHub replaces, for tests of how an action picks its account.
_REAL_PR_TOKEN = github._pr_token
HEAD = "a" * 40
MOVED = "1" * 40
PR_URL = "https://github.com/example-org/example-repo/pull/7"
OTHER_URL = "https://github.com/example-org/example-repo/pull/8"
THIRD_URL = "https://github.com/example-org/example-repo/pull/9"
GITLAB_URL = "https://gitlab.com/example-org/example-repo/-/merge_requests/7"


class FakeGitHub:
    """Answers ``gh pr`` commands and GraphQL requests for pull requests 7, 8 and 9."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state = {"state": "OPEN", "isDraft": False}
        self.heads = {7: HEAD, 8: HEAD, 9: HEAD}
        self.node_states = {7: "OPEN", 8: "OPEN", 9: "OPEN"}
        self.gh_calls: list[tuple[list[str], str | None]] = []
        self.mutations: list[dict[str, Any]] = []
        # The answer to the next ``gh pr`` change: (exit code, stderr).
        self.gh_answer: tuple[int | None, str] = (0, "")
        # Pull request numbers whose auto-merge mutation GitHub refuses, or never answers.
        self.refused: set[int] = set()
        self.lost: set[int] = set()
        # A mutation answer that replaces the confirmed one, by pull request number.
        self.answers: dict[int, Any] = {}
        # The token each read and change ran with: ("view" | "node" | "mutation" | "gh", token).
        self.tokens: list[tuple[str, str | None]] = []
        monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: TOKEN)
        monkeypatch.setattr(github, "_host_args", lambda _root, ref: ["--hostname", ref.host])
        monkeypatch.setattr(github, "_pr_json", self._view)
        monkeypatch.setattr(github, "_gh", self._gh)
        monkeypatch.setattr(github, "_pr_json_write", self._json_write)
        monkeypatch.setattr(
            pr_resource, "_selected_pr", lambda _session, url: PullRequestRef.from_url(url)
        )

    def _view(self, root: str, reference: PullRequestRef, fields: str) -> dict[str, Any]:
        self.tokens.append(("view", github._pr_token(root, reference)))
        return {field: self.state[field] for field in fields.split(",")}

    def _gh(
        self, argv: list[str], *, cwd: str, token: str | None = None
    ) -> tuple[int | None, str, str]:
        self.gh_calls.append((argv, token))
        self.tokens.append(("gh", token))
        code, stderr = self.gh_answer
        return code, "", stderr

    def _json_write(
        self,
        _root: str,
        *,
        host: list[str],
        token: str | None,
        method: str,
        endpoint: str,
        fields: dict[str, Any],
    ) -> tuple[int | None, str, str]:
        assert (method, endpoint) == ("POST", "graphql")
        variables = fields["variables"]
        if "enablePullRequestAutoMerge" not in fields["query"]:
            self.tokens.append(("node", token))
            number = variables["number"]
            node = {"id": f"PR_{number}", "headRefOid": self.heads[number]}
            node["state"] = self.node_states[number]
            return 0, json.dumps({"data": {"repository": {"pullRequest": node}}}), ""
        self.mutations.append(variables["input"])
        self.tokens.append(("mutation", token))
        number = int(variables["input"]["pullRequestId"].removeprefix("PR_"))
        if number in self.lost:
            return None, "", ""
        if number in self.refused:
            errors = [{"type": "UNPROCESSABLE", "message": "Auto merge is not allowed"}]
            return (
                0,
                json.dumps({"data": {"enablePullRequestAutoMerge": None}, "errors": errors}),
                "",
            )
        payload = {"pullRequest": {"id": f"PR_{number}", "autoMergeRequest": {"enabledAt": "t"}}}
        payload = self.answers.get(number, payload)
        return 0, json.dumps({"data": {"enablePullRequestAutoMerge": payload}}), ""

    def pr_commands(self) -> list[list[str]]:
        return [argv for argv, _token in self.gh_calls if argv[:1] == ["pr"]]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    return FakeGitHub(monkeypatch)


def _state(state: object) -> dict[str, Any]:
    return pr_actions.set_pull_request_state(
        "/workspace", session_id="s", pr_url=PR_URL, state=state
    )


def _draft(draft: object) -> dict[str, Any]:
    return pr_actions.set_pull_request_draft(
        "/workspace", session_id="s", pr_url=PR_URL, draft=draft
    )


def _auto_merge(*items: dict[str, Any]) -> dict[str, Any]:
    return pr_actions.enable_auto_merge("/workspace", session_id="s", items=list(items))


def _item(url: str = PR_URL, method: str = "squash", head: str = HEAD) -> dict[str, Any]:
    return {"pr_url": url, "merge_method": method, "expected_head_oid": head}


@pytest.mark.parametrize(
    "current, state, command, answer",
    [
        ("OPEN", "closed", "close", "CLOSED"),
        ("CLOSED", "open", "reopen", "OPEN"),
    ],
)
def test_a_state_change_runs_gh_as_the_pull_requests_account(
    fake: FakeGitHub, current: str, state: str, command: str, answer: str
) -> None:
    fake.state["state"] = current

    result = _state(state)

    assert result == {"object": "session.github.pr_state", "pr_url": PR_URL, "state": answer}
    assert fake.gh_calls == [
        (["pr", command, "7", "-R", "github.com/example-org/example-repo"], TOKEN)
    ]


@pytest.mark.parametrize(
    "current, state, refusal",
    [
        ("MERGED", "closed", "This pull request is merged, so its state can't change."),
        ("MERGED", "open", "This pull request is merged, so its state can't change."),
        ("CLOSED", "closed", "This pull request is already closed."),
        ("OPEN", "open", "This pull request is already open."),
    ],
)
def test_a_state_change_that_wouldnt_change_anything_is_refused_with_nothing_sent(
    fake: FakeGitHub, current: str, state: str, refusal: str
) -> None:
    fake.state["state"] = current

    with pytest.raises(ValueError, match=refusal):
        _state(state)

    assert fake.gh_calls == []


@pytest.mark.parametrize(
    "is_draft, draft, command",
    [(False, True, ["ready", "--undo"]), (True, False, ["ready"])],
    ids=["to-draft", "to-ready"],
)
def test_a_draft_change_runs_gh_pr_ready(
    fake: FakeGitHub, is_draft: bool, draft: bool, command: list[str]
) -> None:
    fake.state["isDraft"] = is_draft

    result = _draft(draft)

    assert result == {"object": "session.github.pr_draft", "pr_url": PR_URL, "is_draft": draft}
    assert fake.pr_commands() == [
        ["pr", *command, "7", "-R", "github.com/example-org/example-repo"]
    ]


@pytest.mark.parametrize(
    "pr_state, is_draft, draft, refusal",
    [
        ("OPEN", True, True, "This pull request is already a draft."),
        ("OPEN", False, False, "This pull request is already ready for review."),
        ("MERGED", False, True, "This pull request is merged, so it can't become a draft."),
    ],
)
def test_a_draft_change_that_wouldnt_change_anything_is_refused_with_nothing_sent(
    fake: FakeGitHub, pr_state: str, is_draft: bool, draft: bool, refusal: str
) -> None:
    fake.state["state"] = pr_state
    fake.state["isDraft"] = is_draft

    with pytest.raises(ValueError, match=refusal):
        _draft(draft)

    assert fake.gh_calls == []


@pytest.mark.parametrize(
    "change, value, refusal",
    [
        (_state, "merged", "Expected state to be closed or open"),
        (_state, None, "Expected state to be closed or open"),
        (_draft, "true", "Expected draft to be true or false"),
    ],
)
def test_a_malformed_lifecycle_change_is_refused_before_github_is_asked(
    fake: FakeGitHub, change: Any, value: object, refusal: str
) -> None:
    with pytest.raises(InvalidPullRequestInput, match=refusal):
        change(value)

    assert fake.gh_calls == []
    assert fake.tokens == []


def test_a_lifecycle_change_of_a_pull_request_github_cant_load_is_refused(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "_pr_json", lambda *_args: None)

    with pytest.raises(ValueError, match="GitHub could not load this pull request"):
        _state("closed")
    with pytest.raises(ValueError, match="GitHub could not load this pull request"):
        _draft(True)

    assert fake.gh_calls == []


def test_a_lifecycle_change_github_refused_reads_as_refused(fake: FakeGitHub) -> None:
    fake.gh_answer = (1, "HTTP 403: Resource not accessible by integration (HTTP 403)")

    with pytest.raises(ValueError, match="cannot close this pull request") as refused:
        _state("closed")

    assert not isinstance(refused.value, UnknownOutcomeError)


@pytest.mark.parametrize(
    "answer",
    [(None, ""), (1, "HTTP 502: Bad Gateway"), (1, "connection reset by peer")],
    ids=["no-answer", "server-error", "reset"],
)
def test_a_lifecycle_change_github_never_confirmed_is_an_unknown_outcome(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, answer: tuple[int | None, str]
) -> None:
    monkeypatch.setattr(github, "_gh_installed", lambda: True)
    fake.gh_answer = answer

    with pytest.raises(UnknownOutcomeError):
        _draft(True)

    assert len(fake.pr_commands()) == 1


def test_a_lifecycle_change_without_gh_says_it_isnt_installed(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "_gh_installed", lambda: False)
    fake.gh_answer = (None, "")

    with pytest.raises(ValueError, match=r"GitHub CLI isn.t installed on github\.com"):
        _state("closed")


def test_a_lifecycle_change_of_another_providers_pull_request_is_refused(
    fake: FakeGitHub,
) -> None:
    with pytest.raises(ValueError, match="supported only for GitHub pull requests"):
        pr_actions.set_pull_request_state(
            "/workspace", session_id="s", pr_url=GITLAB_URL, state="closed"
        )

    assert fake.gh_calls == []


def test_auto_merge_sends_each_pull_request_at_the_head_the_user_saw(fake: FakeGitHub) -> None:
    result = _auto_merge(_item(PR_URL, "squash"), _item(OTHER_URL, "rebase"))

    assert result == {
        "object": "session.github.pr_auto_merge_batch",
        "results": [
            {"pr_url": PR_URL, "enabled_at": "t"},
            {"pr_url": OTHER_URL, "enabled_at": "t"},
        ],
    }
    assert fake.mutations == [
        {"pullRequestId": "PR_7", "mergeMethod": "SQUASH", "expectedHeadOid": HEAD},
        {"pullRequestId": "PR_8", "mergeMethod": "REBASE", "expectedHeadOid": HEAD},
    ]


def test_auto_merge_stops_at_the_first_moved_head_and_sends_nothing_after_it(
    fake: FakeGitHub,
) -> None:
    fake.heads[8] = MOVED

    result = _auto_merge(_item(PR_URL), _item(OTHER_URL), _item(THIRD_URL))

    assert result["results"] == [
        {"pr_url": PR_URL, "enabled_at": "t"},
        {
            "pr_url": OTHER_URL,
            "error": PULL_REQUEST_CHANGED_MESSAGE,
            "reason": "pull_request_changed",
        },
    ]
    assert [mutation["pullRequestId"] for mutation in fake.mutations] == ["PR_7"]


def test_auto_merge_reports_each_refusal_and_goes_on(fake: FakeGitHub) -> None:
    fake.node_states[7] = "CLOSED"
    fake.refused.add(8)

    result = _auto_merge(_item(PR_URL), _item(OTHER_URL))

    assert [entry["pr_url"] for entry in result["results"]] == [PR_URL, OTHER_URL]
    assert result["results"][0] == {
        "pr_url": PR_URL,
        "error": "Only an open pull request can merge automatically.",
        "reason": None,
    }
    assert "Auto merge is not allowed" in result["results"][1]["error"]
    assert "unconfirmed" not in result["results"][1]
    assert [mutation["pullRequestId"] for mutation in fake.mutations] == ["PR_8"]


def test_auto_merge_marks_a_change_github_never_confirmed_and_goes_on(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "_gh_installed", lambda: True)
    fake.lost.add(7)

    result = _auto_merge(_item(PR_URL), _item(OTHER_URL))

    assert result["results"] == [
        {"pr_url": PR_URL, "error": UNKNOWN_OUTCOME_MESSAGE, "reason": None, "unconfirmed": True},
        {"pr_url": OTHER_URL, "enabled_at": "t"},
    ]


@pytest.mark.parametrize(
    "items, refusal",
    [
        ([], "Enable auto-merge for 1 to 20 pull requests at once"),
        ([_item()] * 21, "Enable auto-merge for 1 to 20 pull requests at once"),
        ([_item(method="fast-forward")], "Expected merge_method to be merge, squash, or rebase"),
        ([_item(head="abc")], "Expected expected_head_oid to be a full commit SHA"),
        ([_item(), _item(OTHER_URL), _item()], "Name each pull request once"),
    ],
    ids=["none", "too-many", "bad-method", "bad-head", "repeated"],
)
def test_a_malformed_auto_merge_batch_is_refused_with_nothing_sent(
    fake: FakeGitHub, items: list[dict[str, Any]], refusal: str
) -> None:
    with pytest.raises(InvalidPullRequestInput, match=refusal):
        _auto_merge(*items)

    assert fake.mutations == []
    assert fake.tokens == []


def test_auto_merge_naming_another_providers_pull_request_sends_none(fake: FakeGitHub) -> None:
    with pytest.raises(ValueError, match="supported only for GitHub pull requests"):
        _auto_merge(_item(PR_URL), _item(GITLAB_URL))

    assert fake.mutations == []


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"pullRequest": None},
        {"pullRequest": {"id": "PR_99", "autoMergeRequest": {"enabledAt": "t"}}},
        {"pullRequest": {"id": "PR_7", "autoMergeRequest": None}},
        {"pullRequest": {"id": "PR_7", "autoMergeRequest": {"enabledAt": None}}},
        {"pullRequest": {"id": "PR_7", "autoMergeRequest": {"enabledAt": ""}}},
    ],
    ids=["empty", "no-pr", "another-pr", "no-request", "no-time", "empty-time"],
)
def test_auto_merge_github_doesnt_confirm_for_this_pr_is_unconfirmed_and_goes_on(
    fake: FakeGitHub, answer: dict[str, Any]
) -> None:
    fake.answers[7] = answer

    result = _auto_merge(_item(PR_URL), _item(OTHER_URL))

    assert result["results"] == [
        {"pr_url": PR_URL, "error": UNKNOWN_OUTCOME_MESSAGE, "reason": None, "unconfirmed": True},
        {"pr_url": OTHER_URL, "enabled_at": "t"},
    ]
    assert len(fake.mutations) == 2


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


@pytest.mark.parametrize(
    "change", [lambda: _state("closed"), lambda: _draft(True)], ids=["close", "to-draft"]
)
def test_a_lifecycle_change_with_no_login_chosen_stays_the_account_active_when_it_began(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    """Switching gh's active account after the state check must not change who writes."""
    lookups = _no_login_chosen(monkeypatch, TOKEN, OTHER_TOKEN)

    change()

    assert lookups == ["github.com"]
    assert [kind for kind, _token in fake.tokens] == ["view", "gh"]
    assert {token for _kind, token in fake.tokens} == {TOKEN}


def test_each_auto_merge_item_reads_and_changes_as_one_account(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account switch between two pull requests can't split one's check from its change."""
    lookups = _no_login_chosen(monkeypatch, TOKEN, OTHER_TOKEN)

    _auto_merge(_item(PR_URL), _item(OTHER_URL))

    assert lookups == ["github.com", "github.com"]
    assert fake.tokens == [
        ("node", TOKEN),
        ("mutation", TOKEN),
        ("node", OTHER_TOKEN),
        ("mutation", OTHER_TOKEN),
    ]


def test_a_lifecycle_change_whose_active_account_cant_be_read_is_never_sent(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_login_chosen(monkeypatch, None)

    with pytest.raises(ValueError, match="Couldn't read the host's GitHub sign-in"):
        _state("closed")
    result = _auto_merge(_item(PR_URL))

    assert result["results"][0]["error"].startswith("Couldn't read the host's GitHub sign-in")
    assert "unconfirmed" not in result["results"][0]
    assert fake.tokens == []
