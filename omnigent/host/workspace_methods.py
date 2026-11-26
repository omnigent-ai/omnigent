"""The workspace API's methods, read from its proto, for code that forwards them.

The host's tunnel and the server's route handle every method the same way, so
they share one table instead of one branch per method. A name outside the
table is never dispatched, including a method the schema declares but nothing serves yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.protobuf import message_factory
from google.protobuf.descriptor_pb2 import MethodOptions
from google.protobuf.message import Message

from omnigent.api.workspace.v1alpha1 import workspace_pb2


@dataclass(frozen=True)
class WorkspaceMethod:
    """One WorkspaceService method.

    :param name: As GetCapabilities lists it, e.g. ``"SearchFiles"``.
    :param python_name: The service and client attribute, e.g. ``"search_files"``.
    :param request_type: Its request message class.
    :param response_type: Its response message class.
    :param streaming: Whether it streams responses.
    :param pull_request: Whether it reads or changes one of the session's pull requests:
        its request names one in ``pr_url``, and only the server's route serves it.
    :param no_side_effects: Whether the schema marks it ``NO_SIDE_EFFECTS``. A pull
        request method without the mark changes the pull request.
    """

    name: str
    python_name: str
    request_type: type[Message]
    response_type: type[Message]
    streaming: bool
    pull_request: bool = False
    no_side_effects: bool = False


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# In the schema for clients built against it, but served by no host or server yet: they
# have no host op and no server gate, so a call is refused and GetCapabilities never lists them.
DECLARED_NOT_SERVED: frozenset[str] = frozenset(
    {
        "AddPendingReviewComment",
        "AddReaction",
        "DeleteComment",
        "DeletePendingReviewComment",
        "DequeuePullRequest",
        "DiscardPendingReview",
        "EditComment",
        "EnableAutoMerge",
        "EnqueuePullRequest",
        "GetMergeQueue",
        "GetMergeReadiness",
        "GetPendingReview",
        "GetPullRequestListStats",
        "GetPullRequestStack",
        "ListBranchPullRequests",
        "LookupPullRequest",
        "PreviewReviewerPing",
        "ReadPullRequestImage",
        "RemoveReaction",
        "SearchCompletions",
        "SearchReviewers",
        "SendReviewerPing",
        "SetPullRequestDraft",
        "SetPullRequestState",
        "SetReviewThreadResolved",
        "StartPendingReview",
        "SubmitPendingReview",
        "SubmitReview",
        "SubmitReviewVerdict",
        "UpdatePendingReviewComment",
        "UploadAttachment",
    }
)

WORKSPACE_METHODS: dict[str, WorkspaceMethod] = {
    method.name: WorkspaceMethod(
        name=method.name,
        python_name=_snake_case(method.name),
        request_type=message_factory.GetMessageClass(method.input_type),
        response_type=message_factory.GetMessageClass(method.output_type),
        streaming=method.server_streaming,
        pull_request="pr_url" in method.input_type.fields_by_name,
        no_side_effects=method.GetOptions().idempotency_level == MethodOptions.NO_SIDE_EFFECTS,
    )
    for method in workspace_pb2.DESCRIPTOR.services_by_name["WorkspaceService"].methods
    if method.name not in DECLARED_NOT_SERVED
}
