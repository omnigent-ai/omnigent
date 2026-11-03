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
- With ``label_editing``, the repository's labels are a ``list`` of
  ``{name, color, description}`` with ``has_more``, and a label edit returns a
  ``session.github.pr_labels`` with the PR's resulting ``labels``.
- With ``merging``, merge options are a ``session.github.merge_options`` with
  the allowed ``methods`` (``merge``, ``squash``, ``rebase``) and the ``default``
  to preselect (null when none is allowed), and a merge or disabling
  auto-merge returns a ``session.github.pr_merge`` with ``pr_url``.
- With ``reviewer_editing``, the review candidates are a ``list`` of
  ``{login, kind, name}`` with ``has_more``, and a reviewer edit returns a
  ``session.github.pr_reviewers`` with the PR's resulting ``reviewers``.
- With ``details_editing``, a title or description edit returns a
  ``session.github.pr_details`` with the PR's resulting ``title`` and ``body``.
- With ``attachment_uploads``, an uploaded image or video returns a
  ``session.github.pr_attachment`` with the ``url`` to embed in the description.
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
- The dispatcher adds descriptor ``provider_display`` to info and associations.
- ``checks`` may also count ``cancelled`` and ``skipped`` jobs, with matching ``runs``
  buckets. Providers that do not expose cancelled and skipped as distinct buckets omit
  these counts.
- ``pr`` may carry ``author_id``, the PR author's stable id on the provider; optional,
  may be null.
- A comment may carry ``author_id``, the author's stable id on the provider; optional,
  may be null.
- ``pr`` may carry ``labels`` of ``{name, color, description}``; ``color`` is six
  lowercase hex digits or null, and ``description`` may be null.
- ``pr`` may carry ``merge_state_status``, GitHub's ``mergeStateStatus`` (e.g.
  ``"CLEAN"``, ``"BLOCKED"``, ``"UNKNOWN"``) or null, and ``auto_merge``, null
  when auto-merge is off, else ``{merge_method, enabled_by, enabled_at}`` with
  ``merge_method`` ``"MERGE"``, ``"SQUASH"``, or ``"REBASE"``. Both are omitted
  when the provider or its CLI cannot report them.
- A changed file's ``lines_added`` and ``lines_removed`` may be null.
- Report lookup failures in ``warnings``. Mark incomplete checks/comments with
  ``checks.partial`` / ``comments_partial``; changed files use ``has_more`` or ``warning``.
- An unavailable PR diff has an empty ``patch`` and ``unavailable_reason``, with
  an optional user-facing ``message``. ``pr_outside_workspace`` names that specific case.

GitHub also keeps the legacy top-level ``gh_available``, ``authenticated``,
``accounts``, and ``selected_account``, which ``auth`` supersedes. They are
deprecated and will be removed in 0.19.0. Other providers omit them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from omnigent.runner.session_prs import PullRequestRef

# Wire ids keep the GitHub name for every provider.
INFO_OBJECT = "session.github.info"
CHANGED_FILE_OBJECT = "session.github.changed_file"
FILE_DIFF_OBJECT = "session.github.file_diff"
PR_DIFF_OBJECT = "session.github.pr_diff"
PR_LABELS_OBJECT = "session.github.pr_labels"
MERGE_OPTIONS_OBJECT = "session.github.merge_options"
PR_MERGE_OBJECT = "session.github.pr_merge"
PR_REVIEWERS_OBJECT = "session.github.pr_reviewers"
PR_DETAILS_OBJECT = "session.github.pr_details"
PR_ATTACHMENT_OBJECT = "session.github.pr_attachment"
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
    :ivar label_editing: The user can add and remove the PR's labels through
        :meth:`PullRequestFacet.repo_labels` and :meth:`PullRequestFacet.edit_labels`.
    :ivar merging: The user can merge the PR through
        :meth:`PullRequestFacet.merge_options` and :meth:`PullRequestFacet.merge`,
        and turn off its auto-merge with :meth:`PullRequestFacet.disable_auto_merge`.
    :ivar reviewer_editing: The user can request and withdraw reviews through
        :meth:`PullRequestFacet.review_candidates` and
        :meth:`PullRequestFacet.edit_reviewers`.
    :ivar comment_posting: The user can post top-level comments through
        :meth:`PullRequestFacet.create_comment`.
    :ivar review_threads: Include published review conversations in PR info via
        the optional :class:`ReviewThreadsFacet`; page additional threads on demand.
    :ivar review_thread_replies: Publish replies through :class:`ReviewThreadsFacet`.
    :ivar details_editing: The user can edit the PR's title and description
        through :meth:`PullRequestFacet.edit_details`.
    :ivar attachment_uploads: The user can upload images and videos to embed in
        the description through :meth:`PullRequestFacet.upload_attachment`.
    """

    account_switching: bool
    base_remote_selection: bool
    line_counts: bool
    linked_pr_diff: bool
    label_editing: bool = False
    merging: bool = False
    reviewer_editing: bool = False
    comment_posting: bool = False
    review_threads: bool = False
    review_thread_replies: bool = False
    review_thread_creation: bool = False
    details_editing: bool = False
    attachment_uploads: bool = False

    def to_json(self) -> dict[str, bool]:
        """Return the capabilities as the payload's ``capabilities`` object."""
        return asdict(self)


@runtime_checkable
class ReviewThreadsFacet(Protocol):
    """Optional review conversation support, independent of the base provider protocol."""

    def review_diff(
        self, root: str, reference: PullRequestRef, *, head_sha: str, base_sha: str
    ) -> dict[str, Any]: ...

    def review_threads(
        self, root: str, reference: PullRequestRef, *, after: str, head_sha: str, base_sha: str
    ) -> dict[str, Any]: ...

    def review_thread_comments(
        self, root: str, reference: PullRequestRef, *, thread_id: str, after: str
    ) -> dict[str, Any]: ...

    def reply_to_review_thread(
        self, root: str, reference: PullRequestRef, *, thread_id: str, body: str
    ) -> dict[str, Any]: ...

    def create_review_thread(
        self, root: str, reference: PullRequestRef, *, body: str, anchor: dict[str, Any]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReviewerRef:
    """A user or team a review is requested from.

    :ivar login: A user's login, e.g. ``"octocat"``, or a team's slug, e.g. ``"core"``.
    :ivar kind: ``"user"`` or ``"team"``.
    """

    login: str
    kind: Literal["user", "team"]


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
    :ivar output_eligible: False when redirection, piping, or background execution
        prevents assigning the shared output to this invocation.
    """

    raw_tokens: tuple[str, ...]
    invocation_tokens: tuple[str, ...]
    output_eligible: bool = True


@dataclass(frozen=True)
class ShellPrOp:
    """What one shell segment does to a pull request, as its provider reads it.

    The observer records PRs only when at least one op tracks. The PRs are
    ``created`` when every tracking op creates, else ``worked_on``. It records
    the ``target`` of each tracking op. It takes PR URLs from the shared output
    only when every recognized op tracks and none is ``content_only``.

    :ivar tracks: The command changes the PR, e.g. create, edit, merge, close,
        or a review that approves or requests changes. Reads and comment-only
        commands do not track.
    :ivar creates: The command creates a PR. Implies ``tracks``.
    :ivar target: The PR that the command's arguments name, or ``None`` when
        they name none, e.g. on create or for the current branch's PR.
    :ivar content_only: The command prints only PR content (body, title, or
        diff), so URLs in its output do not identify the PR.
    :ivar parse_output: Optional network-free parser for command-specific output.
        Replaces generic JSON/bare-URL extraction for this operation.
    :ivar parse_result: Optional parser that also needs structured tool results.
        Takes precedence over ``parse_output`` and generic extraction.
    :ivar resolve_result: Optional lookup for identities absent from command output.
        Runs in the background after success, separately from immediate PR operations.
    """

    tracks: bool
    creates: bool
    target: PullRequestRef | None
    content_only: bool
    parse_output: Callable[[str], Sequence[PullRequestRef]] | None = None
    parse_result: Callable[[object], Sequence[PullRequestRef]] | None = None
    resolve_result: Callable[[object], Sequence[PullRequestRef]] | None = None


@runtime_checkable
class PullRequestFacet(Protocol):
    """The provider-specific pull request steps behind the session PR panel.

    ``root`` is the absolute path of the session workspace. A ``reference`` of
    ``None`` means the PR of the workspace's current branch. Methods block and
    can run concurrently in worker threads. Report a forge or credential
    failure explicitly, for example ``pr: None`` with ``warnings``,
    ``auth.authenticated: False``, or a file-list ``warning``. Raise ``ValueError``
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

    def repo_labels(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Return the labels the PR's repository offers, as a ``list`` with ``has_more``.

        The orchestrator calls it only with ``label_editing``.
        """
        ...

    def edit_labels(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        add: Sequence[str],
        remove: Sequence[str],
    ) -> dict[str, Any]:
        """Add and remove existing repository labels on the PR.

        The orchestrator calls it only with ``label_editing``, with disjoint
        non-empty name lists. Removing a label the PR lacks is not an error.

        :returns: A ``session.github.pr_labels`` with the PR's resulting ``labels``.
        :raises ValueError: With a user-facing message when the provider refuses.
        """
        ...

    def merge_options(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Return a ``session.github.merge_options`` for the PR's repository.

        The orchestrator calls it only with ``merging``.
        """
        ...

    def merge(
        self, root: str, reference: PullRequestRef, *, merge_method: str, expected_head: str
    ) -> dict[str, Any]:
        """Merge the PR with ``merge_method``, refusing if its head is not ``expected_head``.

        The orchestrator calls it only with ``merging``, a method from
        ``merge``, ``squash``, and ``rebase``, and a full commit SHA.

        :returns: A ``session.github.pr_merge``.
        :raises ValueError: With a user-facing message when the provider refuses.
        """
        ...

    def disable_auto_merge(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Turn off the PR's auto-merge.

        The orchestrator calls it only with ``merging``.

        :returns: A ``session.github.pr_merge``.
        :raises ValueError: With a user-facing message when the provider refuses.
        """
        ...

    def review_candidates(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        """Return the users and teams a review can be requested from, as a ``list``.

        The orchestrator calls it only with ``reviewer_editing``.
        """
        ...

    def edit_reviewers(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        add: Sequence[ReviewerRef],
        remove: Sequence[ReviewerRef],
    ) -> dict[str, Any]:
        """Request reviews from ``add`` and withdraw the pending requests of ``remove``.

        The orchestrator calls it only with ``reviewer_editing``, with disjoint
        lists that are not both empty. Withdrawing a request the PR lacks is not an error.

        :returns: A ``session.github.pr_reviewers`` with the PR's resulting ``reviewers``.
        :raises ValueError: With a user-facing message when the provider refuses.
        """
        ...

    def create_comment(self, root: str, reference: PullRequestRef, *, body: str) -> dict[str, Any]:
        """Post a top-level conversation comment when ``comment_posting`` is supported."""
        ...

    def edit_details(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        title: str | None,
        body: str | None,
    ) -> dict[str, Any]:
        """Replace the PR's title and/or description; ``None`` leaves that field as is.

        The orchestrator calls it only with ``details_editing``, with a non-blank
        ``title`` or a ``body`` set.

        :returns: A ``session.github.pr_details`` with the PR's resulting ``title`` and ``body``.
        :raises ValueError: With a user-facing message when the provider refuses.
        """
        ...

    def upload_attachment(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        name: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Upload an image or video for the PR's description without changing the PR.

        The orchestrator calls it only with ``attachment_uploads``, with a
        ``content_type`` it detected from ``content`` and a ``name`` whose
        extension matches it.

        :returns: A ``session.github.pr_attachment`` with the ``url`` to embed.
        :raises ValueError: With a user-facing message when the provider refuses.
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
