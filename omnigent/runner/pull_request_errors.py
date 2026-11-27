"""Refusals of a pull request operation, each with a machine-readable reason.

They subclass ``ValueError``, so callers that catch ``ValueError`` keep working. The
workspace API maps them to Connect codes and sends the reason.
"""

from __future__ import annotations

PULL_REQUEST_CHANGED_MESSAGE = "This pull request changed. Refresh to see the new commits."
UNKNOWN_OUTCOME_MESSAGE = (
    "GitHub may have made this change. Check the pull request before trying again."
)


class PullRequestError(ValueError):
    """A refused pull request operation, with a user-facing message.

    :ivar reason: A machine-readable reason, or ``None`` when the refusal has none.
    """

    reason: str | None = None


class PullRequestChangedError(PullRequestError):
    """The pull request's head moved since the user saw it; nothing was written."""

    reason = "pull_request_changed"

    def __init__(self) -> None:
        super().__init__(PULL_REQUEST_CHANGED_MESSAGE)


class InvalidPullRequestInput(PullRequestError):
    """The request's fields are malformed; the code host was never asked."""

    reason = "invalid_input"


class CommentChangedError(PullRequestError):
    """The comment's text changed since the client read it; nothing was written."""

    reason = "comment_changed"

    def __init__(self) -> None:
        super().__init__("The comment changed. Refresh to see its latest text.")


class UnknownOutcomeError(PullRequestError):
    """A write was sent, but GitHub never confirmed whether it was made.

    It must not read as a refusal: a client that retries would repeat the change.
    """

    def __init__(self) -> None:
        super().__init__(UNKNOWN_OUTCOME_MESSAGE)
