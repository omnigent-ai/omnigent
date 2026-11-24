"""What a shared session's pull request info shows to someone other than its owner.

Every change to a session's pull requests runs as its host's code-host account, which is
the session owner's, so on a shared session only that owner may make one; an admin doesn't
count. Everyone else sees the pull requests without the controls that change them, and
without the host's other accounts. An editor still sees which account the owner's changes
come from; a viewer, who can't act in the session, doesn't.
"""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum
from typing import Any

from omnigent.runner.git_providers import ProviderCapabilities

OWNER_ONLY_CHANGES_MESSAGE = (
    "Only the session's owner can change its pull requests. "
    "Open the pull request on GitHub to make changes there."
)
# Capabilities that change the pull request, or the host's account and checkout.
OWNER_CAPABILITIES = frozenset(
    {
        "account_switching",
        "attachment_uploads",
        "base_remote_selection",
        "comment_posting",
        "details_editing",
        "label_editing",
        "merge_as_account",
        "merging",
        "review_thread_creation",
        "review_thread_replies",
        "reviewer_editing",
    }
)
# Capabilities that only read; everyone who sees the info keeps them.
READ_CAPABILITIES = frozenset(
    {"commit_history", "line_counts", "linked_pr_diff", "review_threads"}
)
# What a client assumes for a host that predates ``capabilities``.
_LEGACY_READ_CAPABILITIES = {"line_counts": True, "linked_pr_diff": True}
_ALL_CAPABILITIES = tuple(field.name for field in fields(ProviderCapabilities))


class PullRequestViewer(StrEnum):
    """Who reads a session's pull request info, decided once per request."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


def info_for_viewer(info: dict[str, Any], viewer: PullRequestViewer) -> dict[str, Any]:
    """``info`` as ``viewer`` may see it; the owner's is unchanged.

    :param info: A ``session.github.info`` payload, e.g. ``{"auth": {"accounts": [...]}}``.
    :param viewer: Who reads it.
    :returns: For anyone but the owner, a copy with ``owner_only_changes``, no account list
        and a full ``capabilities`` object whose change capabilities are all false.
    """
    if viewer is PullRequestViewer.OWNER:
        return info
    hidden = {"accounts"} | ({"selected_account"} if viewer is PullRequestViewer.VIEWER else set())
    shown: dict[str, Any] = {key: value for key, value in info.items() if key not in hidden}
    shown["owner_only_changes"] = True
    auth = info.get("auth")
    if isinstance(auth, dict):
        shown["auth"] = {**auth, **dict.fromkeys(hidden)}
    shown["capabilities"] = _read_only_capabilities(info.get("capabilities"))
    return shown


def _read_only_capabilities(capabilities: object) -> dict[str, bool]:
    """Every known capability, with only the reads kept; unknown names are turned off.

    Sent even when the host sent none, so a client's defaults for an older host
    can't offer a change the server refuses.
    """
    reported = capabilities if isinstance(capabilities, dict) else _LEGACY_READ_CAPABILITIES
    names = set(_ALL_CAPABILITIES) | {name for name in reported if isinstance(name, str)}
    return {
        name: name in READ_CAPABILITIES and reported.get(name) is True for name in sorted(names)
    }
