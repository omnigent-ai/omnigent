"""Pull request facet protocol: the provider-specific half of the session PR panel.

``omnigent.runner.pr_resource`` keeps the provider-neutral steps: the selected
PR, tracked PRs and their cached titles, branch inference, attach and remove,
and routing preferences. It calls a :class:`PullRequestFacet` for each step
that talks to a forge. ``omnigent.runner.pr_observer`` calls the observer
methods to find PRs in completed tool calls. A provider's ``pull_requests``
facet module (see :func:`omnigent.git_providers.load_facet`) exposes one
instance::

    PULL_REQUESTS: PullRequestFacet = ForgePullRequests()

The observer loads every provider's facet when a tool call completes, so a
facet module imports its panel-only dependencies inside the methods that use them.

Payload contract:

- The info payload's ``object`` is ``"session.github.info"`` for every
  provider. It is a stable wire id, not a provider name.
- Every provider fills the neutral info fields: ``available`` (with ``reason``
  when false), ``branch``, ``base_ref``, ``repo`` (``{"name_with_owner": ...}``),
  ``pr``, and ``selected_pr_url`` for a tracked PR.
- ``pr`` has ``number``, ``url`` (a URL that its provider's descriptor
  parses), ``title``, ``state`` (``"OPEN"``, ``"MERGED"``, or ``"CLOSED"``),
  ``is_draft``, ``author``, ``base_ref``, ``head_ref``, ``head_sha``,
  ``base_sha``, ``checks`` (``passing``, ``failing``, ``pending``, ``total``,
  and ``runs`` of ``{name, bucket, url}``), ``body``, and ``comments`` of
  ``{author, body, created_at, url}``.
- Changed files are a ``list`` of ``session.github.changed_file`` objects:
  ``path``, ``name``, ``status`` (``created``, ``modified``, ``deleted``, or
  ``renamed``), ``lines_added``, and ``lines_removed``. The PR diff is a
  ``session.github.pr_diff`` with ``patch``. One file's diff is a
  ``session.github.file_diff`` with ``path``, ``before``, and ``after``.

Additive fields:

- ``provider``: the provider id, e.g. ``"github"``; null for an unsupported remote.
- ``auth``: a :class:`PullRequestAuth`; null when ``available`` is false.
- ``capabilities``: :meth:`ProviderCapabilities.to_json`; null for an unsupported remote.
- ``remote_host``: only with ``reason: "unsupported_remote"``.
- PR associations in ``prs`` carry ``provider``.
- ``pr`` may carry ``author_id``, the PR author's stable id on the provider; optional,
  may be null.
- A comment may carry ``author_id``, the author's stable id on the provider; optional,
  may be null.
- A changed file's ``lines_added`` and ``lines_removed`` may be null.
- The PR diff may carry ``unavailable_reason: "pr_outside_workspace"`` with an
  empty ``patch``.

GitHub also keeps the legacy top-level ``gh_available``, ``authenticated``,
``accounts``, and ``selected_account``, which ``auth`` supersedes. They are
deprecated and will be removed in 0.19.0. Other providers omit them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict, runtime_checkable

from omnigent.runner.session_prs import PullRequestRef

# Wire ids keep the GitHub name for every provider.
INFO_OBJECT = "session.github.info"
CHANGED_FILE_OBJECT = "session.github.changed_file"
FILE_DIFF_OBJECT = "session.github.file_diff"
PR_DIFF_OBJECT = "session.github.pr_diff"
# Info ``reason`` when no provider recognizes the workspace's git remote.
UNSUPPORTED_REMOTE = "unsupported_remote"
# PR diff ``unavailable_reason`` when the PR's repository is not the workspace's.
PR_OUTSIDE_WORKSPACE = "pr_outside_workspace"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Panel features a provider supports, sent as the payload's ``capabilities``.

    :ivar account_switching: The user can choose among several signed-in CLI
        accounts: ``auth.accounts`` lists them and
        :meth:`PullRequestFacet.set_preference` accepts ``account``.
    :ivar base_remote_selection: The user can choose the base remote, which
        :meth:`PullRequestFacet.set_preference` accepts as ``remote``.
    :ivar line_counts: Changed files carry added and removed line counts;
        without it both counts are null.
    :ivar linked_pr_diff: A PR linked from outside the workspace's repository
        still has a diff; without it :meth:`PullRequestFacet.pr_diff` returns
        ``unavailable_reason: "pr_outside_workspace"`` for such a PR.
    """

    account_switching: bool
    base_remote_selection: bool
    line_counts: bool
    linked_pr_diff: bool

    def to_json(self) -> dict[str, bool]:
        """Return the capabilities as the payload's ``capabilities`` object."""
        return asdict(self)


class CliStatus(TypedDict):
    """The CLI a provider runs, e.g. ``{"name": "gh", "available": True}``."""

    name: str
    available: bool


class PullRequestAuth(TypedDict):
    """The info payload's ``auth`` block: whether the provider can reach the PR.

    ``hint`` is a short next step for the user when ``authenticated`` is false,
    such as a sign-in command. ``cli`` is null for a provider that runs no CLI.
    ``accounts`` lists the accounts the user can choose among (GitHub: ``login``,
    ``active``, ``state``, ``host``), or is null when none were listed.
    ``selected_account`` is the account the provider acts as, when known.
    """

    authenticated: bool
    hint: str | None
    cli: CliStatus | None
    accounts: list[dict[str, Any]] | None
    selected_account: str | None


@dataclass(frozen=True)
class ShellSegment:
    """One simple command from a completed shell tool call.

    The observer splits the command on ``;``, ``&``, ``|``, and newlines, and
    unwraps nested shell ``-c`` strings, so each segment runs one program.

    :ivar raw_tokens: The segment as lexed, keeping leading environment
        assignments such as ``GH_HOST=example.com`` and command wrappers.
    :ivar invocation_tokens: The real command and its arguments, after
        ``real_invocation_tokens`` from ``omnigent.policies.builtins._shell``
        drops those prefixes. Never empty.
    """

    raw_tokens: tuple[str, ...]
    invocation_tokens: tuple[str, ...]


@dataclass(frozen=True)
class ShellPrOp:
    """What one shell segment does to a pull request, as its provider reads it.

    The observer records PRs only when at least one op tracks. The PRs are
    ``created`` when every tracking op creates, else ``worked_on``. It records
    the ``target`` of each tracking op. It takes PR URLs from the shared output
    only when every recognized op tracks and, if there is only one op, that op
    is not ``content_only``.

    :ivar tracks: The command changes the PR, e.g. create, edit, merge, close,
        or a review that approves or requests changes. Reads and comment-only
        commands do not track.
    :ivar creates: The command creates a PR. Implies ``tracks``.
    :ivar target: The PR that the command's arguments name, or ``None`` when
        they name none, e.g. on create or for the current branch's PR.
    :ivar content_only: The command prints only PR content (body, title, or
        diff), so URLs in its output do not identify the PR.
    """

    tracks: bool
    creates: bool
    target: PullRequestRef | None
    content_only: bool


@runtime_checkable
class PullRequestFacet(Protocol):
    """The provider-specific pull request steps behind the session PR panel.

    ``root`` is the absolute path of the session workspace. A ``reference`` of
    ``None`` means the PR of the workspace's current branch. Methods block and
    can run concurrently in worker threads. Report a forge or credential
    failure in the result, for example ``pr: None`` with
    ``auth.authenticated: False``, or an empty file list. Raise ``ValueError``
    only with a message for the user; the routes return it as HTTP 400.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """The panel features this provider supports."""
        ...

    def workspace_info(self, root: str) -> dict[str, Any]:
        """Return the info payload for the workspace's current branch and its PR.

        Outside a git checkout, return ``available: false`` with
        ``reason: "not_a_git_repo"``.
        """
        ...

    def reference_info(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Return the info payload for one tracked PR, independent of the checkout.

        ``selected_pr_url`` is ``reference.url``. When the PR cannot be read,
        ``pr`` is null and ``auth`` tells why.
        """
        ...

    def titles_available(self, root: str) -> bool:
        """Return whether :meth:`pr_title` can run, e.g. whether the provider's CLI is installed.

        When false, the orchestrator neither looks up nor re-caches this
        provider's titles.
        """
        ...

    def pr_title(
        self, root: str, reference: PullRequestRef, deadline: float
    ) -> tuple[str | None, bool]:
        """Look up one PR's title, giving up at ``deadline``. Never raises.

        :param deadline: A ``time.monotonic()`` value that no request may outlast.
        :returns: ``(title, timed_out)``. ``title`` is the stripped, non-empty
            title, or ``None`` when the lookup failed. ``timed_out`` is true only
            when the deadline ended the lookup; the orchestrator then retries
            sooner than after other failures.
        """
        ...

    def verify_accessible(self, root: str, reference: PullRequestRef) -> None:
        """Check that the host's credentials can read the PR before it is attached.

        :raises ValueError: With a user-facing message when they cannot.
        """
        ...

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        """Run after the orchestrator associates a PR it inferred from the workspace branch.

        Not called when the user removed that PR from the session. GitHub
        copies the workspace's account preference to the PR here.
        """
        ...

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """Return the PR's changed files as a ``list`` of ``session.github.changed_file``.

        The list is empty when no PR resolves.
        """
        ...

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """Return the whole PR as one unified diff, a ``session.github.pr_diff``.

        ``patch`` is empty when no PR resolves. Without ``linked_pr_diff``, a PR
        from outside the workspace's repository gets an empty ``patch`` and
        ``unavailable_reason: "pr_outside_workspace"``.
        """
        ...

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
        """Return one file's full content before and after, a ``session.github.file_diff``.

        :param path: Repository-relative path at the head revision.
        :param base: Base branch of the local checkout diff when ``reference``
            is ``None``; empty means the base of the branch's PR.
        :param previous_path: The path at the base revision, for a rename.
        :param head_sha: The head revision the panel shows. Raise ``ValueError``
            when the PR's head is now different, so the panel never mixes revisions.
        :param base_sha: The base revision the panel shows, checked the same way.
        """
        ...

    def set_preference(
        self,
        root: str,
        reference: PullRequestRef | None,
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        """Save the user's account or base remote choice.

        With a ``reference``, ``account`` applies to that PR only and ``remote``
        is ignored; without one, both apply to the workspace. ``None`` leaves a
        choice unchanged, and an empty ``account`` clears it.

        :raises ValueError: When asked for a choice that the provider's
            capabilities exclude.
        """
        ...

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        """Return one op per segment that runs this provider's PR command, in order.

        Report read commands too, with ``tracks`` false: the observer takes PRs
        from shared output only when every recognized command tracks.
        """
        ...

    def pr_from_object(self, obj: Mapping[str, object]) -> PullRequestRef | None:
        """Return the PR that provider-specific fields of a JSON object in shell output name.

        The observer tries the generic ``html_url`` and ``url`` fields first.
        """
        ...

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        """Extract the PRs from a successful call of one of this provider's MCP tools.

        The observer asks each provider in registration order, uses the first
        answer that is not ``None``, and removes duplicate URLs.

        :returns: ``(references, created)``, where ``references`` can be empty
            and ``created`` is true when the tool created them; or ``None`` when
            ``tool_name`` is not one of this provider's PR tools.
        """
        ...


def unsupported_remote_info(host: str) -> dict[str, Any]:
    """Return the info payload for a workspace whose git remote no provider recognizes.

    :param host: The remote's host, e.g. ``"gitlab.com"``.
    :returns: A new ``session.github.info`` object with ``available: false``.
    """
    return {
        "object": INFO_OBJECT,
        "available": False,
        "reason": UNSUPPORTED_REMOTE,
        "remote_host": host,
        "provider": None,
        "auth": None,
        "capabilities": None,
        "repo": None,
        "pr": None,
    }
