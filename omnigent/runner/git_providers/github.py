"""GitHub pull request facet, backed by the ``gh`` CLI.

The panel steps live in :mod:`omnigent.runner.github_resource`. Each method
imports that module when it runs and calls through it, so patches on the module
apply, and the tool-call observer, which loads every facet, skips the panel code.
The observer steps live in :mod:`omnigent.runner.git_providers.github_observer`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.runner.git_providers import (
    ProviderCapabilities,
    PullRequestAuth,
    PullRequestFacet,
    ShellPrOp,
    ShellSegment,
    github_observer,
)
from omnigent.runner.session_prs import PullRequestRef

_PROVIDER_ID = "github"
# ``gh pr diff -R host/owner/repo`` reads any PR, so a linked PR keeps its diff.
_CAPABILITIES = ProviderCapabilities(
    account_switching=True,
    base_remote_selection=True,
    line_counts=True,
    linked_pr_diff=True,
)


def _auth(info: Mapping[str, Any]) -> PullRequestAuth:
    """Build ``auth`` from the legacy top-level fields, which the payload keeps."""
    return {
        # @deprecated, remove in 0.19.0: the top-level ``authenticated`` field.
        "authenticated": bool(info.get("authenticated")),
        # The panel shows GitHub's own sign-in guidance.
        "hint": None,
        # @deprecated, remove in 0.19.0: the top-level ``gh_available`` field.
        "cli": {"name": "gh", "available": bool(info.get("gh_available"))},
        # @deprecated, remove in 0.19.0: the top-level ``accounts`` field.
        "accounts": info.get("accounts"),
        # @deprecated, remove in 0.19.0: the top-level ``selected_account`` field.
        "selected_account": info.get("selected_account"),
    }


def _with_provider_fields(info: dict[str, Any]) -> dict[str, Any]:
    """Add ``provider``, ``auth``, and ``capabilities`` to a ``github_resource`` payload."""
    info["provider"] = _PROVIDER_ID
    info["auth"] = None if info.get("available") is False else _auth(info)
    info["capabilities"] = _CAPABILITIES.to_json()
    return info


class GitHubPullRequests:
    """GitHub's pull request steps for the session panel."""

    capabilities = _CAPABILITIES

    def workspace_info(self, root: str) -> dict[str, Any]:
        """Return the info payload for the workspace branch's PR."""
        from omnigent.runner import github_resource

        return _with_provider_fields(github_resource._workspace_github_info(root))

    def reference_info(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Return the info payload for one tracked PR."""
        from omnigent.runner import github_resource

        return _with_provider_fields(github_resource._reference_info(root, reference))

    def titles_available(self, root: str) -> bool:  # noqa: ARG002 - gh is host-wide
        """Return whether the ``gh`` CLI is installed."""
        from omnigent.runner import github_resource

        return github_resource._gh_installed()

    def pr_title(
        self, root: str, reference: PullRequestRef, deadline: float
    ) -> tuple[str | None, bool]:
        """Look up one PR's title before ``deadline``."""
        from omnigent.runner import github_resource

        return github_resource._pr_title_before(root, reference, deadline)

    def verify_accessible(self, root: str, reference: PullRequestRef) -> None:
        """Raise unless ``gh`` on the host can read the PR."""
        from omnigent.runner import github_resource

        github_resource._verify_pr_access(root, reference)

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        """Give a branch-inferred PR the workspace's account preference."""
        from omnigent.runner import github_resource

        github_resource._adopt_workspace_account(root, reference)

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """List the PR's changed files from GitHub."""
        from omnigent.runner import github_resource

        return github_resource._pr_changed_files(root, reference)

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """Return the PR's patch from ``gh pr diff``."""
        from omnigent.runner import github_resource

        return github_resource._pr_patch(root, reference)

    def file_diff(
        self,
        root: str,
        reference: PullRequestRef | None,
        path: str,
        *,
        base: str,
        previous_path: str | None,
        head_sha: str | None,
        base_sha: str | None,
    ) -> dict[str, Any]:
        """Return one file's full content before and after."""
        from omnigent.runner import github_resource

        return github_resource._pr_file_diff(
            root,
            reference,
            path,
            base=base,
            previous_path=previous_path,
            head_sha=head_sha,
            base_sha=base_sha,
        )

    def set_preference(
        self,
        root: str,
        reference: PullRequestRef | None,
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        """Save a gh account choice and the workspace's base remote."""
        from omnigent.runner import github_resource

        github_resource._set_preference(root, reference, account=account, remote=remote)

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        """Return one op per ``gh pr`` or ``gh api`` segment."""
        return github_observer.shell_pr_operations(segments)

    def pr_from_object(
        self,
        obj: Mapping[str, object],  # noqa: ARG002
    ) -> PullRequestRef | None:
        """Name no PR: GitHub output carries the generic ``html_url`` and ``url`` fields."""
        return None

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        """Read GitHub's pull request MCP tools and the ``write_api_call`` proxy."""
        return github_observer.mcp_prs(tool_name, arguments, result)


PULL_REQUESTS: PullRequestFacet = GitHubPullRequests()
