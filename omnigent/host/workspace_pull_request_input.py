"""Checks on a pull request call's own fields, which the server and the host both make.

The server refuses such a call before it builds a vouch or sends a frame. The host
refuses it again, so a frame from any server is held to the same rules.
"""

from __future__ import annotations

from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError


def refuse_unserved_request(method: str, request: Any) -> None:
    """Refuse a request for something this API doesn't do, before any other work.

    :param method: The method's name, e.g. ``"SetPullRequestAccount"``.
    :param request: Its request message.
    :raises ConnectError: INVALID_ARGUMENT, with the reason.
    """
    reason: str | None = None
    if method == "SetPullRequestAccount":
        # Without a PR the account and remote would change for the whole workspace.
        if not request.pr_url:
            reason = "Expected a pull request URL"
        elif request.remote:
            reason = "Changing the base repository isn't available here"
    elif method == "MergePullRequestAsAccount":
        # Without an account the merge would run as the saved one, not the confirmed one.
        if not request.account.strip():
            reason = "Expected an account to merge as"
    elif method == "ListLabels" and request.text:
        reason = "Label search isn't available on this host"
    if reason is not None:
        raise ConnectError(Code.INVALID_ARGUMENT, reason)
