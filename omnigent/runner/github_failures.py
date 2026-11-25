"""User-facing messages for ``gh`` and GraphQL failures, with no token, URL or argv in them.

A write that was sent counts as refused only when GitHub definitely refused it. Any
other failure after the send, such as a 5xx or a reset connection, is an unknown
outcome: the change may have been made, so it must never read as safe to retry.
"""

from __future__ import annotations

import re

from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import UnknownOutcomeError

NOT_INSTALLED = "GitHub CLI isn't installed on {host}."
SIGNED_OUT = "Sign in on {host} with `gh auth login`."
MISSING_SCOPE = (
    "The GitHub login on {host} needs the `repo` scope. Run `gh auth refresh -s repo` there."
)
SSO = "Authorize the login on {host} for SSO."
RATE_LIMITED = "GitHub is rate limiting this login on {host}. Try again later."

_SCOPE = re.compile(
    r"INSUFFICIENT_SCOPES|X-Accepted-OAuth-Scopes|required scopes?|missing.*scope", re.I
)
_SSO = re.compile(r"SAML|\bSSO\b|single sign-on", re.I)
_RATE = re.compile(r"rate limit|HTTP 429", re.I)
_SIGNED_OUT = re.compile(
    r"gh auth login|not logged in|not logged into|HTTP 401|Bad credentials", re.I
)
# The HTTP statuses that mean GitHub refused the request without making the change.
_REFUSED_STATUS = re.compile(r"\(HTTP (?:401|403|404|422)\)")
# GraphQL errors GitHub reports when it may have stopped partway, e.g. a timeout.
_GRAPHQL_INTERRUPTED = re.compile(r"timeout|timed out|Something went wrong", re.I)
# GraphQL error types that say GitHub failed on its side, whatever the message.
_INTERRUPTED_TYPES = frozenset({"INTERNAL", "TIMEOUT", "SERVICE_UNAVAILABLE"})
# URLs, paths and host names: a repository or host named like ``sso`` must not read as a refusal.
_ADDRESSES = re.compile(r"\S*(?:/|://)\S*|\b[\w-]+(?:\.[\w-]+)+(?::\d+)?\b")


def _diagnostic(stderr: str) -> str:
    """``gh``'s message with every address taken out, so only its wording is matched."""
    return _ADDRESSES.sub(" ", stderr)


def unknown_outcome() -> UnknownOutcomeError:
    """The error for a write whose result GitHub never confirmed."""
    return UnknownOutcomeError()


def refusal(stderr: str, *, host: str, action: str) -> ValueError:
    """Turn a failed ``gh`` call's stderr into the user-facing error for ``action``."""
    diagnostic = _diagnostic(stderr)
    if _SCOPE.search(diagnostic):
        return ValueError(MISSING_SCOPE.format(host=host))
    if _SSO.search(diagnostic):
        return ValueError(SSO.format(host=host))
    if _RATE.search(diagnostic):
        return ValueError(RATE_LIMITED.format(host=host))
    if _SIGNED_OUT.search(diagnostic):
        return ValueError(SIGNED_OUT.format(host=host))
    return ValueError(github._merge_error(stderr, action=action))


def definitely_refused(stderr: str) -> bool:
    """Whether a sent write's ``gh`` stderr shows GitHub refused it without making it."""
    diagnostic = _diagnostic(stderr)
    return any(
        pattern.search(diagnostic)
        for pattern in (_REFUSED_STATUS, _SCOPE, _SSO, _RATE, _SIGNED_OUT)
    )


def write_failure(stderr: str, *, host: str, action: str) -> ValueError:
    """The error for a sent write that failed: a refusal only when GitHub definitely refused."""
    if definitely_refused(stderr):
        return refusal(stderr, host=host, action=action)
    return unknown_outcome()


def graphql_refusal(errors: list[object], *, host: str, action: str) -> ValueError:
    """The error for a GraphQL mutation GitHub answered with ``errors``."""
    messages = [str(error.get("message", "")) for error in errors if isinstance(error, dict)]
    text = "\n".join(messages)
    internal = any(
        isinstance(error, dict) and error.get("type") in _INTERRUPTED_TYPES for error in errors
    )
    if not messages or internal or _GRAPHQL_INTERRUPTED.search(text):
        return unknown_outcome()
    return refusal(text, host=host, action=action)


def not_installed(host: str) -> ValueError:
    """The refusal when ``gh`` itself can't run."""
    return ValueError(NOT_INSTALLED.format(host=host))
