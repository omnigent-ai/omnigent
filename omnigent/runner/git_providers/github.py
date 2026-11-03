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
    ReviewerRef,
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
    label_editing=True,
    merging=True,
    reviewer_editing=True,
    comment_posting=True,
    review_threads=True,
    review_thread_replies=True,
    review_thread_creation=True,
    details_editing=True,
    attachment_uploads=True,
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

    def review_diff(
        self, root: str, reference: PullRequestRef, *, head_sha: str, base_sha: str
    ) -> dict[str, Any]:
        """Fetch a patch whose revisions match the displayed review conversations."""
        from omnigent.runner import github_resource

        return github_resource._pr_patch_at_revision(
            root, reference, head_sha=head_sha, base_sha=base_sha
        )

    def review_threads(
        self, root: str, reference: PullRequestRef, *, after: str, head_sha: str, base_sha: str
    ) -> dict[str, Any]:
        """Read the first or a subsequent review-thread page."""
        from omnigent.runner import github_review_threads

        return github_review_threads.read_threads(
            root, reference, after=after, head_sha=head_sha, base_sha=base_sha
        )

    def review_thread_comments(
        self, root: str, reference: PullRequestRef, *, thread_id: str, after: str
    ) -> dict[str, Any]:
        """Load another page in a published thread."""
        from omnigent.runner import github_review_threads

        return github_review_threads.read_comments(
            root, reference, thread_id=thread_id, after=after
        )

    def reply_to_review_thread(
        self, root: str, reference: PullRequestRef, *, thread_id: str, body: str
    ) -> dict[str, Any]:
        """Publish a reply immediately to its original review conversation."""
        from omnigent.runner import github_review_threads

        return github_review_threads.reply(root, reference, thread_id=thread_id, body=body)

    def create_review_thread(
        self, root: str, reference: PullRequestRef, *, body: str, anchor: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish a new line conversation as a submitted comment review."""
        from omnigent.runner import github_review_threads

        return github_review_threads.create_thread(root, reference, body=body, anchor=anchor)

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

    def repo_labels(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """List the repository's labels with ``gh label list``."""
        from omnigent.runner import github_resource

        return github_resource._repo_labels(root, reference)

    def edit_labels(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        add: Sequence[str],
        remove: Sequence[str],
    ) -> dict[str, Any]:
        """Add and remove the PR's labels through GitHub's issue labels API."""
        from omnigent.runner import github_resource

        return github_resource._edit_labels(root, reference, add=add, remove=remove)

    def merge_options(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Read the repository's allowed merge methods with ``gh repo view``."""
        from omnigent.runner import github_resource

        return github_resource._merge_options(root, reference)

    def merge(
        self, root: str, reference: PullRequestRef, *, merge_method: str, expected_head: str
    ) -> dict[str, Any]:
        """Merge the PR with ``gh pr merge --match-head-commit``."""
        from omnigent.runner import github_resource

        return github_resource._merge_pr(
            root, reference, merge_method=merge_method, expected_head=expected_head
        )

    def disable_auto_merge(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Turn off auto-merge with ``gh pr merge --disable-auto``."""
        from omnigent.runner import github_resource

        return github_resource._disable_auto_merge(root, reference)

    def review_candidates(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """List the repository's assignable users and its teams."""
        from omnigent.runner import github_resource

        return github_resource._review_candidates(root, reference)

    def edit_reviewers(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        add: Sequence[ReviewerRef],
        remove: Sequence[ReviewerRef],
    ) -> dict[str, Any]:
        """Request and withdraw reviews through GitHub's requested reviewers API."""
        from omnigent.runner import github_resource

        return github_resource._edit_reviewers(root, reference, add=add, remove=remove)

    def create_comment(self, root: str, reference: PullRequestRef, *, body: str) -> dict[str, Any]:
        """Post through GitHub's issue comments API as the selected account."""
        from omnigent.runner import github_resource

        return github_resource._create_comment(root, reference, body=body)

    def edit_details(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        title: str | None,
        body: str | None,
    ) -> dict[str, Any]:
        """Update the title and description through GitHub's pull requests API."""
        from omnigent.runner import github_resource

        return github_resource._edit_details(root, reference, title=title, body=body)

    def upload_attachment(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        name: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Upload through GitHub's user attachments endpoint, as ``gh --attach`` does."""
        from omnigent.runner import github_resource

        return github_resource._upload_attachment(
            root, reference, name=name, content_type=content_type, content=content
        )

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
