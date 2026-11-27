"""Who may call each workspace API method through the server.

Checked before a call is forwarded, against the caller's access to the session. A
method missing from the table is refused without reaching the host, so a new method
stays unreachable until it is given a level here.
"""

from __future__ import annotations

from enum import StrEnum


class WorkspaceGate(StrEnum):
    """The access a method needs.

    ``BROWSE`` is the file routes' gate: edit access, or read access once the owner
    shares the workspace's files. ``SESSION_OWNER`` passes only the holder of the
    session's own owner grant, or anyone where permissions are off; an admin doesn't.
    """

    READ = "read"
    BROWSE = "browse"
    SESSION_OWNER = "session_owner"


# The pull request reads keep their REST routes' gates. GetPullRequest's overview carries
# the host account's identities, so only the session's owner reads it here for now. The
# changes, and the reads that feed their controls, act as the host's account: the owner's.
METHOD_GATES: dict[str, WorkspaceGate] = {
    "GetCapabilities": WorkspaceGate.BROWSE,
    "ListDirectory": WorkspaceGate.BROWSE,
    "ReadFile": WorkspaceGate.BROWSE,
    "StatPath": WorkspaceGate.BROWSE,
    "SearchFiles": WorkspaceGate.BROWSE,
    "SearchText": WorkspaceGate.BROWSE,
    "ResolveWorkingCopyBase": WorkspaceGate.BROWSE,
    "ListWorkingCopyChanges": WorkspaceGate.BROWSE,
    "ReadBaseFile": WorkspaceGate.BROWSE,
    "ReadLinkText": WorkspaceGate.BROWSE,
    "ListRepositoryRefs": WorkspaceGate.BROWSE,
    "ListBranchCommits": WorkspaceGate.BROWSE,
    "ResolveRepositoryContext": WorkspaceGate.BROWSE,
    "GetPullRequest": WorkspaceGate.SESSION_OWNER,
    "ListPullRequestFiles": WorkspaceGate.BROWSE,
    "GetPullRequestDiff": WorkspaceGate.BROWSE,
    "ListPullRequestCommits": WorkspaceGate.BROWSE,
    "GetCommitDiff": WorkspaceGate.BROWSE,
    "ComparePullRequestRange": WorkspaceGate.BROWSE,
    "ListReviewThreads": WorkspaceGate.BROWSE,
    "ListReviewThreadComments": WorkspaceGate.READ,
    "ListLabels": WorkspaceGate.SESSION_OWNER,
    "ListReviewerCandidates": WorkspaceGate.SESSION_OWNER,
    # Searches as the host's account, for the owner's reviewer picker and comment editor.
    "SearchReviewers": WorkspaceGate.SESSION_OWNER,
    "SearchCompletions": WorkspaceGate.SESSION_OWNER,
    "GetMergeOptions": WorkspaceGate.SESSION_OWNER,
    "CreateComment": WorkspaceGate.SESSION_OWNER,
    "CreateReviewThread": WorkspaceGate.SESSION_OWNER,
    "ReplyToReviewThread": WorkspaceGate.SESSION_OWNER,
    "UpdatePullRequestDetails": WorkspaceGate.SESSION_OWNER,
    "UpdateLabels": WorkspaceGate.SESSION_OWNER,
    "UpdateReviewers": WorkspaceGate.SESSION_OWNER,
    "SetPullRequestAccount": WorkspaceGate.SESSION_OWNER,
    "MergePullRequest": WorkspaceGate.SESSION_OWNER,
    "MergePullRequestAsAccount": WorkspaceGate.SESSION_OWNER,
    "DisableAutoMerge": WorkspaceGate.SESSION_OWNER,
    "EnableAutoMerge": WorkspaceGate.SESSION_OWNER,
    "SetPullRequestState": WorkspaceGate.SESSION_OWNER,
    "SetPullRequestDraft": WorkspaceGate.SESSION_OWNER,
    # Reviews act as the host's account, and its pending review holds unsubmitted drafts.
    "GetPendingReview": WorkspaceGate.SESSION_OWNER,
    "StartPendingReview": WorkspaceGate.SESSION_OWNER,
    "AddPendingReviewComment": WorkspaceGate.SESSION_OWNER,
    "UpdatePendingReviewComment": WorkspaceGate.SESSION_OWNER,
    "DeletePendingReviewComment": WorkspaceGate.SESSION_OWNER,
    "SubmitPendingReview": WorkspaceGate.SESSION_OWNER,
    "DiscardPendingReview": WorkspaceGate.SESSION_OWNER,
    "SubmitReview": WorkspaceGate.SESSION_OWNER,
    "SubmitReviewVerdict": WorkspaceGate.SESSION_OWNER,
    "SetReviewThreadResolved": WorkspaceGate.SESSION_OWNER,
    "EditComment": WorkspaceGate.SESSION_OWNER,
    "DeleteComment": WorkspaceGate.SESSION_OWNER,
    "AddReaction": WorkspaceGate.SESSION_OWNER,
    "RemoveReaction": WorkspaceGate.SESSION_OWNER,
    "UploadAttachment": WorkspaceGate.SESSION_OWNER,
    # The queue acts as the host's account, and its state is read for the owner's controls.
    "GetMergeQueue": WorkspaceGate.SESSION_OWNER,
    "EnqueuePullRequest": WorkspaceGate.SESSION_OWNER,
    "DequeuePullRequest": WorkspaceGate.SESSION_OWNER,
    # The stack lists the repository's other pull requests, read as the host's account.
    "GetPullRequestStack": WorkspaceGate.SESSION_OWNER,
    # Read any pull request the host's account can see, so the owner can choose what to link.
    "LookupPullRequest": WorkspaceGate.SESSION_OWNER,
    "GetPullRequestListStats": WorkspaceGate.SESSION_OWNER,
    # Fetched with the host account's token, which sees its private repositories' images.
    "ReadPullRequestImage": WorkspaceGate.SESSION_OWNER,
}


def gate_for(method: str) -> WorkspaceGate | None:
    """The access ``method`` needs, or ``None`` for a method the server doesn't forward."""
    return METHOD_GATES.get(method)


def callable_methods(*, browse: bool, session_owner: bool) -> frozenset[str]:
    """The methods a caller with read access may call, given what else they clear.

    :param browse: Whether the caller clears the file routes' gate.
    :param session_owner: Whether the caller holds the session's own owner grant.
    """
    cleared = {WorkspaceGate.READ}
    if browse:
        cleared.add(WorkspaceGate.BROWSE)
    if session_owner:
        cleared.add(WorkspaceGate.SESSION_OWNER)
    return frozenset(name for name, gate in METHOD_GATES.items() if gate in cleared)
