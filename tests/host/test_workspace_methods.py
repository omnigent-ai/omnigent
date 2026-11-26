"""The workspace API's method table, read from its proto."""

from __future__ import annotations

from google.protobuf.descriptor_pb2 import MethodOptions

from omnigent.api.workspace.v1alpha1 import workspace_pb2
from omnigent.host.workspace_methods import (
    NON_PULL_REQUEST_METHODS,
    WORKSPACE_METHODS,
    WorkspaceMethod,
    unclassified_methods,
)

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
# Reviewed by hand: each changes pull requests the session links, or reads what its
# edit controls offer. Only the session's owner may call them. A new one is added here.
_PULL_REQUEST_OWNER_METHODS = frozenset(
    {
        "ListLabels",
        "ListReviewerCandidates",
        "GetMergeOptions",
        "CreateComment",
        "CreateReviewThread",
        "ReplyToReviewThread",
        "UpdatePullRequestDetails",
        "UpdateLabels",
        "UpdateReviewers",
        "SetPullRequestAccount",
        "MergePullRequest",
        "MergePullRequestAsAccount",
        "DisableAutoMerge",
        "EnableAutoMerge",
        "SetPullRequestState",
        "SetPullRequestDraft",
        "GetPendingReview",
        "StartPendingReview",
        "AddPendingReviewComment",
        "UpdatePendingReviewComment",
        "DeletePendingReviewComment",
        "SubmitPendingReview",
        "DiscardPendingReview",
        "SubmitReview",
        "SubmitReviewVerdict",
        "SetReviewThreadResolved",
        "EditComment",
        "DeleteComment",
        "AddReaction",
        "RemoveReaction",
        "UploadAttachment",
        "GetMergeQueue",
        "EnqueuePullRequest",
        "DequeuePullRequest",
        "GetPullRequestStack",
        "ReadPullRequestImage",
    }
)
# The owner methods that stream their answer; every other pull request method is unary.
_PULL_REQUEST_OWNER_STREAMS = frozenset({"ReadPullRequestImage"})
# The owner methods that change the pull request; a lost answer may hide a change.
_PULL_REQUEST_WRITES = _PULL_REQUEST_OWNER_METHODS - {
    "ListLabels",
    "ListReviewerCandidates",
    "GetMergeOptions",
    "GetPendingReview",
    "GetMergeQueue",
    "GetPullRequestStack",
    "ReadPullRequestImage",
}
# Only the server's call frame carries these; a request message never does.
_SERVER_ONLY_FIELDS = frozenset(
    {"session_id", "vouch", "server_tracked", "default_pr_url", "account_rights"}
)


def test_every_pull_request_method_is_a_reviewed_read_or_owner_method() -> None:
    marked = {name for name, method in WORKSPACE_METHODS.items() if method.pull_request}
    assert not _PULL_REQUEST_READS & _PULL_REQUEST_OWNER_METHODS
    assert marked == _PULL_REQUEST_READS | _PULL_REQUEST_OWNER_METHODS


def test_only_the_reads_have_no_side_effects() -> None:
    """A write is never served over GET, and the server never reports its lost answer as safe."""
    service = workspace_pb2.DESCRIPTOR.services_by_name["WorkspaceService"]
    pull_requests = {name for name, method in WORKSPACE_METHODS.items() if method.pull_request}
    marked = {
        name
        for name in pull_requests
        if service.methods_by_name[name].GetOptions().idempotency_level
        == MethodOptions.NO_SIDE_EFFECTS
    }
    assert marked == pull_requests - _PULL_REQUEST_WRITES
    assert {name for name in pull_requests if WORKSPACE_METHODS[name].no_side_effects} == marked


def test_only_the_image_read_streams() -> None:
    pull_requests = _PULL_REQUEST_READS | _PULL_REQUEST_OWNER_METHODS
    streamed = {name for name in pull_requests if WORKSPACE_METHODS[name].streaming}
    assert streamed == _PULL_REQUEST_OWNER_STREAMS


def test_no_request_message_can_name_a_session_or_vouch_for_a_pull_request() -> None:
    for method in WORKSPACE_METHODS.values():
        fields = set(method.request_type.DESCRIPTOR.fields_by_name)
        assert not fields & _SERVER_ONLY_FIELDS, (method.name, fields & _SERVER_ONLY_FIELDS)


def test_the_review_changes_have_side_effects_and_the_pending_review_read_has_none() -> None:
    """Pinned by name, so a schema edit can't make a review change look safe to repeat."""
    review_changes = {
        "StartPendingReview",
        "AddPendingReviewComment",
        "UpdatePendingReviewComment",
        "DeletePendingReviewComment",
        "SubmitPendingReview",
        "DiscardPendingReview",
        "SubmitReview",
        "SubmitReviewVerdict",
        "SetReviewThreadResolved",
        "EditComment",
        "DeleteComment",
        "AddReaction",
        "RemoveReaction",
    }
    assert not any(WORKSPACE_METHODS[name].no_side_effects for name in review_changes)
    lifecycle_changes = {"SetPullRequestState", "SetPullRequestDraft", "EnableAutoMerge"}
    assert not any(WORKSPACE_METHODS[name].no_side_effects for name in lifecycle_changes)
    assert WORKSPACE_METHODS["GetPendingReview"].no_side_effects


def test_every_served_method_reads_a_named_pull_request_or_is_a_workspace_read() -> None:
    assert unclassified_methods(WORKSPACE_METHODS) == []
    assert set(WORKSPACE_METHODS) >= NON_PULL_REQUEST_METHODS
    assert not any(WORKSPACE_METHODS[name].pull_request for name in NON_PULL_REQUEST_METHODS)


def test_a_served_method_without_a_pull_request_must_be_classified() -> None:
    """A method that names no PR would be scoped to no session; serving it fails the import."""
    template = WORKSPACE_METHODS["EditComment"]
    unnamed = WorkspaceMethod(
        name="ExampleUnscopedChange",
        python_name="example_unscoped_change",
        request_type=template.request_type,
        response_type=template.response_type,
        streaming=False,
        pull_request=False,
    )
    assert unclassified_methods({**WORKSPACE_METHODS, "ExampleUnscopedChange": unnamed}) == [
        "ExampleUnscopedChange"
    ]


def test_a_batch_naming_its_pull_requests_in_a_list_is_a_pull_request_change() -> None:
    """The server vouches for each pull request a batch names, as for one in ``pr_url``."""
    batch = WORKSPACE_METHODS["EnableAutoMerge"]
    assert "pr_url" not in batch.request_type.DESCRIPTOR.fields_by_name
    assert batch.pull_request
    assert not batch.no_side_effects
