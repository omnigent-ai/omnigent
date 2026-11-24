"""The workspace API's method table, read from its proto."""

from __future__ import annotations

from omnigent.host.workspace_methods import WORKSPACE_METHODS

# Reviewed by hand: each reads one pull request the session links. A new one is added here.
_PULL_REQUEST_READS = frozenset(
    {
        "GetPullRequest",
        "ListPullRequestFiles",
        "GetPullRequestDiff",
        "ListPullRequestCommits",
        "GetCommitDiff",
        "ComparePullRequestRange",
        "ListReviewThreads",
        "ListReviewThreadComments",
    }
)
# Only the server's call frame carries these; a request message never does.
_SERVER_ONLY_FIELDS = frozenset({"session_id", "vouch", "server_tracked", "default_pr_url"})


def test_the_pull_request_methods_are_the_reviewed_reads() -> None:
    marked = {name for name, method in WORKSPACE_METHODS.items() if method.pull_request}
    assert marked == _PULL_REQUEST_READS


def test_no_pull_request_method_streams() -> None:
    assert not any(WORKSPACE_METHODS[name].streaming for name in _PULL_REQUEST_READS)


def test_no_request_message_can_name_a_session_or_vouch_for_a_pull_request() -> None:
    for method in WORKSPACE_METHODS.values():
        fields = set(method.request_type.DESCRIPTOR.fields_by_name)
        assert not fields & _SERVER_ONLY_FIELDS, (method.name, fields & _SERVER_ONLY_FIELDS)
