"""The field checks the server and the host both make on a pull request call."""

from __future__ import annotations

import pytest
from connectrpc.code import Code

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host.workspace_pull_request_input import (
    INVALID_INPUT_REASON,
    PullRequestRefused,
    refuse_unserved_request,
)


@pytest.mark.parametrize(
    ("branch", "refusal"),
    [
        ("", "branch name must not be empty"),
        ("-x", "branch name must not start with '-'"),
        ("a..b", "branch name must not contain '..'"),
        ("a b", "branch name 'a b' contains an invalid character"),
        ("x" * 256, "The branch name is longer than 255 bytes"),
        # 128 characters, but 256 bytes: the limit is on bytes.
        ("é" * 128, "The branch name is longer than 255 bytes"),
        ("HEAD", "branch name must not be 'HEAD'"),
    ],
    ids=["empty", "flag", "dots", "space", "long", "long-in-bytes", "head"],
)
def test_a_branch_list_naming_no_branch_is_refused(branch: str, refusal: str) -> None:
    with pytest.raises(PullRequestRefused) as refused:
        refuse_unserved_request(
            "ListBranchPullRequests", pb.ListBranchPullRequestsRequest(branch=branch)
        )

    assert refused.value.code == Code.INVALID_ARGUMENT
    assert refused.value.message.startswith(refusal)
    assert refused.value.reason == INVALID_INPUT_REASON


@pytest.mark.parametrize(
    "branch",
    ["main", "x" * 255, "é" * 127 + "x", "HEADS", "release/HEAD", "feature/login"],
    ids=["main", "255-bytes", "multibyte", "heads", "nested-head", "nested"],
)
def test_a_branch_list_at_the_limits_is_allowed(branch: str) -> None:
    refuse_unserved_request(
        "ListBranchPullRequests", pb.ListBranchPullRequestsRequest(branch=branch)
    )
