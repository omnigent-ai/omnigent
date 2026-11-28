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
    :param pull_request: Whether it reads or changes the session's pull requests: its
        request names one in ``pr_url``, or several in ``pull_requests``, and only the
        server's route serves it.
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


# The request fields that name pull requests; the server vouches for each one named.
PULL_REQUEST_FIELDS = frozenset({"pr_url", "pull_requests"})
# Pull request methods that find pull requests beyond the session's own. Only the server's
# route serves them, and it sends them with no vouch, since they need none of the session's.
PULL_REQUEST_FINDERS: frozenset[str] = frozenset(
    {"GetPullRequestListStats", "ListBranchPullRequests", "LookupPullRequest"}
)


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# In the schema for clients built against it, but served by no host or server yet: they
# have no host op and no server gate, so a call is refused and GetCapabilities never lists them.
DECLARED_NOT_SERVED: frozenset[str] = frozenset(
    {
        "GetMergeReadiness",
        "PreviewReviewerPing",
        "SendReviewerPing",
    }
)

# The served methods that read the workspace, not a pull request. Any other served method
# must name its pull request in ``pr_url``, so it is scoped to the call frame's session.
NON_PULL_REQUEST_METHODS: frozenset[str] = frozenset(
    {
        "GetCapabilities",
        "ListDirectory",
        "ReadFile",
        "StatPath",
        "SearchFiles",
        "SearchText",
        "ResolveWorkingCopyBase",
        "ListWorkingCopyChanges",
        "ReadBaseFile",
        "ReadLinkText",
        "ListRepositoryRefs",
        "ListBranchCommits",
        "ResolveRepositoryContext",
    }
)


def unclassified_methods(served: dict[str, WorkspaceMethod]) -> list[str]:
    """Served methods that name no pull request yet aren't listed as reading the workspace."""
    return sorted(
        name
        for name, method in served.items()
        if not method.pull_request and name not in NON_PULL_REQUEST_METHODS
    )


WORKSPACE_METHODS: dict[str, WorkspaceMethod] = {
    method.name: WorkspaceMethod(
        name=method.name,
        python_name=_snake_case(method.name),
        request_type=message_factory.GetMessageClass(method.input_type),
        response_type=message_factory.GetMessageClass(method.output_type),
        streaming=method.server_streaming,
        pull_request=method.name in PULL_REQUEST_FINDERS
        or bool(PULL_REQUEST_FIELDS & set(method.input_type.fields_by_name)),
        no_side_effects=method.GetOptions().idempotency_level == MethodOptions.NO_SIDE_EFFECTS,
    )
    for method in workspace_pb2.DESCRIPTOR.services_by_name["WorkspaceService"].methods
    if method.name not in DECLARED_NOT_SERVED
}

if _unclassified := unclassified_methods(WORKSPACE_METHODS):
    # Serving one would leave its session to chance; it must be classified first.
    raise RuntimeError(f"Unclassified workspace methods: {', '.join(_unclassified)}")
