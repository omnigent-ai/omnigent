"""Forgejo and Gitea pull request panel adapter over ``/api/v1`` and the host's CLI logins."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from omnigent.git_providers import EnvInstances, ParsedRemote, provider_display
from omnigent.git_providers.forgejo import ForgejoProvider
from omnigent.runner.git_providers import (
    CHANGED_FILE_OBJECT,
    FILE_DIFF_OBJECT,
    INFO_OBJECT,
    PR_DIFF_OBJECT,
    ProviderCapabilities,
    PullRequestFacet,
    ReviewerRef,
    forgejo_observer,
    local_git,
)
from omnigent.runner.session_prs import PullRequestRef

if TYPE_CHECKING:
    from omnigent.runner.forgejo_client import ForgejoClient

_CAPABILITIES = ProviderCapabilities(False, False, True, True)
_COMMIT = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
_HEX_COLOR = re.compile(r"[0-9a-f]{6}")
# ``(state, merged)``; Forgejo reports a merged pull request as closed.
_STATES = {("open", False): "OPEN", ("closed", False): "CLOSED", ("closed", True): "MERGED"}
# Commit status states; a warning does not block, like GitHub's neutral conclusion.
_BUCKETS = {
    "success": "passing",
    "warning": "passing",
    "pending": "pending",
    "error": "failing",
    "failure": "failing",
}
# Changed-file statuses other than these, such as ``changed``, are modifications.
_FILE_STATUSES = {
    "added": "created",
    "copied": "created",
    "deleted": "deleted",
    "renamed": "renamed",
}
_DENIED = frozenset({401, 403, 404})


class _DiscoveryError(ValueError):
    """Branch discovery failed after the source repository was readable."""


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _git(root: str, args: list[str]) -> tuple[int | None, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _branch(root: str) -> str | None:
    rc, branch = _git(root, ["branch", "--show-current"])
    return branch.strip() or None if rc == 0 else None


def _parse_remote(url: str | None) -> ParsedRemote | None:
    return ForgejoProvider().parse_remote_url(url, EnvInstances()) if url else None


def _remotes(root: str) -> list[ParsedRemote]:
    """Prefer the branch's upstream, then origin and the remaining remotes."""
    remotes = local_git.remote_urls(lambda args: _git(root, args))
    branch = _branch(root)
    _, upstream = _git(root, ["config", f"branch.{branch}.remote"]) if branch else (None, "")
    remotes.sort(key=lambda item: (item[0] != upstream.strip(), item[0] != "origin"))
    found: dict[tuple[str, str], ParsedRemote] = {}
    for _, url in remotes:
        if parsed := _parse_remote(url):
            found.setdefault((parsed.host, parsed.repository), parsed)
    return list(found.values())


def _source_remote(root: str, remotes: list[ParsedRemote]) -> ParsedRemote:
    """The remote the branch pushes to: pushRemote, then pushDefault, then the first remote."""
    branch = _branch(root)
    _, push = _git(root, ["config", f"branch.{branch}.pushRemote"])
    if not push.strip():
        _, push = _git(root, ["config", "remote.pushDefault"])
    named = dict(local_git.remote_urls(lambda args: _git(root, args)))
    return _parse_remote(named.get(push.strip())) or remotes[0]


def _client(host: str, deadline: float | None = None) -> ForgejoClient:
    from omnigent.runner.forgejo_client import ForgejoClient

    return ForgejoClient(host, deadline=deadline)


def _repo(reference: PullRequestRef | ParsedRemote) -> list[str]:
    return ["repos", *reference.repository.split("/")]


def _pull(client: ForgejoClient, reference: PullRequestRef) -> dict[str, Any]:
    pull = client.object(*_repo(reference), "pulls", reference.number)
    returned = forgejo_observer.reference(pull.get("html_url"))
    if (
        pull.get("number") != reference.number
        or (pull.get("state"), bool(pull.get("merged"))) not in _STATES
        or returned is None
        or returned.url != reference.url
    ):
        raise ValueError("Forgejo returned an invalid pull request identity or state.")
    return pull


def _head(pull: dict[str, Any]) -> tuple[str, object]:
    head = _object(pull.get("head"))
    return str(_object(head.get("repo")).get("full_name") or "").lower(), head.get("ref")


def _discover(
    client: ForgejoClient, source: ParsedRemote, remotes: list[ParsedRemote], branch: str
) -> tuple[ForgejoClient, PullRequestRef, dict[str, Any]] | None:
    """Find the one open pull request whose head is the source repository's branch."""
    repository = client.object(*_repo(source))
    parent = _parse_remote(_object(repository.get("parent")).get("html_url"))
    candidates = [remote for remote in remotes if remote.host == source.host]
    if parent and parent.host == source.host and parent not in candidates:
        candidates.append(parent)
    failure = None
    for candidate in candidates:
        try:
            pulls, partial = client.pages(*_repo(candidate), "pulls", state="open")
            if partial:
                raise ValueError(
                    "Forgejo returned an incomplete pull request list. Attach the intended PR URL."
                )
            matches = [pull for pull in pulls if _head(pull) == (source.repository, branch)]
            if len(matches) > 1:
                raise ValueError(
                    "Several pull requests use this branch. Attach the intended PR URL."
                )
            if not matches:
                continue
            selected = forgejo_observer.reference(matches[0].get("html_url"))
            if selected is None or (selected.host, selected.repository) != (
                candidate.host,
                candidate.repository,
            ):
                raise ValueError("Forgejo returned an invalid pull request identity.")
            return client, selected, _pull(client, selected)
        except ValueError as exc:
            failure = exc
    if failure is not None:
        raise _DiscoveryError(str(failure)) from failure
    return None


@contextmanager
def _resolved(
    root: str, reference: PullRequestRef | None, deadline: float | None = None
) -> Iterator[tuple[ForgejoClient, PullRequestRef, dict[str, Any]] | None]:
    """Yield the client, reference, and pull request for a reference or the current branch."""
    if reference is not None:
        with _client(reference.host, deadline) as client:
            yield client, reference, _pull(client, reference)
        return
    branch = _branch(root)
    if not branch:
        raise ValueError("Check out a branch or attach the intended pull request URL.")
    remotes = _remotes(root)
    if not remotes:
        raise ValueError("Configure a Forgejo remote or attach the intended pull request URL.")
    source = _source_remote(root, remotes)
    with _client(source.host, deadline) as client:
        yield _discover(client, source, remotes, branch)


def _sign_in_hint(host: str) -> str:
    return (
        f"Run `fj -H {host} auth login` or `tea login add` on the execution host "
        "to read private repositories."
    )


def _base_info(root: str, reference: PullRequestRef | None) -> dict[str, Any]:
    repo = reference or next(iter(_remotes(root)), None)
    host = repo.host if repo else ForgejoProvider.default_hosts[0]
    return {
        "object": INFO_OBJECT,
        "available": True,
        "provider": "forgejo",
        "provider_display": provider_display("forgejo"),
        "branch": _branch(root),
        "base_ref": None,
        "repo": {"name_with_owner": repo.repository} if repo else None,
        "pr": None,
        "selected_pr_url": reference.url if reference else None,
        "capabilities": _CAPABILITIES.to_json(),
        "warnings": [],
        "auth": {
            "authenticated": False,
            "hint": _sign_in_hint(host),
            "cli": None,
            "accounts": None,
            "selected_account": None,
        },
    }


def _optional(
    warnings: list[str],
    label: str,
    read: Callable[..., tuple[list[dict[str, Any]], bool]],
    *segments: str | int,
) -> tuple[list[dict[str, Any]], bool]:
    """Read a list that the summary can show without, noting a failure as a warning."""
    try:
        values, partial = read(*segments)
    except ValueError:
        values, partial = [], True
    if partial:
        warnings.append(f"Forgejo {label} are incomplete. Refresh to retry.")
    return values, partial


def _comment_url(ref: PullRequestRef, value: object) -> str:
    """Keep the forge's anchor only when it points into this pull request."""
    linked = forgejo_observer.reference(value)
    return str(value) if linked is not None and linked.url == ref.url else ref.url


def _comments(
    client: ForgejoClient, ref: PullRequestRef, warnings: list[str]
) -> tuple[list[dict[str, Any]], bool]:
    def unpaginated(*segments: str | int) -> tuple[list[dict[str, Any]], bool]:
        return client.items(*segments), False

    notes, notes_partial = _optional(
        warnings, "comments", unpaginated, *_repo(ref), "issues", ref.number, "comments"
    )
    reviews, reviews_partial = _optional(
        warnings, "reviews", client.pages, *_repo(ref), "pulls", ref.number, "reviews"
    )
    entries = [(note, note.get("created_at")) for note in notes] + [
        (review, review.get("submitted_at"))
        for review in reviews
        if str(review.get("body") or "").strip()
    ]
    comments = [
        {
            "author": _object(entry.get("user")).get("login"),
            "author_id": str(_object(entry.get("user")).get("id") or "") or None,
            "body": str(entry.get("body") or ""),
            "created_at": created_at,
            "url": _comment_url(ref, entry.get("html_url")),
        }
        for entry, created_at in entries
    ]
    comments.sort(key=lambda comment: str(comment["created_at"] or ""))
    return comments, notes_partial or reviews_partial


def _checks(
    client: ForgejoClient, ref: PullRequestRef, head_sha: object, warnings: list[str]
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    partial = False
    if isinstance(head_sha, str) and head_sha:
        try:
            combined = client.object(*_repo(ref), "commits", head_sha, "status", limit=50)
            statuses = [s for s in combined.get("statuses") or [] if isinstance(s, dict)]
            total = combined.get("total_count")
            partial = isinstance(total, int) and total > len(statuses)
        except ValueError:
            statuses, partial = [], True
        if partial:
            warnings.append("Forgejo checks are incomplete. Refresh to retry.")
        runs = [
            {
                "name": str(status.get("context") or "Status"),
                "bucket": _BUCKETS.get(str(status.get("status")), "pending"),
                "url": status.get("target_url") or None,
            }
            for status in statuses
        ]
    counts = {
        bucket: sum(run["bucket"] == bucket for run in runs)
        for bucket in ("passing", "failing", "pending")
    }
    return {**counts, "total": len(runs), "runs": runs, "partial": partial}


def _label(value: object) -> dict[str, Any]:
    label = _object(value)
    color = str(label.get("color") or "").removeprefix("#").lower()
    return {
        "name": str(label.get("name") or ""),
        "color": color if _HEX_COLOR.fullmatch(color) else None,
        "description": label.get("description") or None,
    }


def _pr_payload(
    client: ForgejoClient, ref: PullRequestRef, pull: dict[str, Any], warnings: list[str]
) -> dict[str, Any]:
    head, base, user = (_object(pull.get(key)) for key in ("head", "base", "user"))
    comments, comments_partial = _comments(client, ref, warnings)
    return {
        "number": ref.number,
        "url": ref.url,
        "title": str(pull.get("title") or ""),
        "state": _STATES[(pull["state"], bool(pull.get("merged")))],
        "is_draft": bool(pull.get("draft")),
        "author": user.get("login"),
        "author_id": str(user.get("id") or "") or None,
        "base_ref": base.get("ref"),
        "head_ref": head.get("ref"),
        "head_sha": head.get("sha"),
        "base_sha": pull.get("merge_base"),
        "body": pull.get("body"),
        "labels": [_label(label) for label in pull.get("labels") or []],
        "comments": comments,
        "comments_partial": comments_partial,
        "checks": _checks(client, ref, head.get("sha"), warnings),
    }


def _info(root: str, reference: PullRequestRef | None = None) -> dict[str, Any]:
    from omnigent.runner.forgejo_client import ForgejoError

    info = _base_info(root, reference)
    if reference is None and _git(root, ["rev-parse", "--show-toplevel"])[0] != 0:
        return {**info, "available": False, "reason": "not_a_git_repo"}
    auth = info["auth"]
    try:
        with _resolved(root, reference) as resolved:
            auth.update(authenticated=True, hint=None)
            if resolved:
                client, ref, pull = resolved
                info.update(repo={"name_with_owner": ref.repository}, selected_pr_url=ref.url)
                info["pr"] = _pr_payload(client, ref, pull, info["warnings"])
                info["base_ref"] = info["pr"]["base_ref"]
    except ValueError as exc:
        denied = isinstance(exc, ForgejoError) and exc.status in _DENIED
        if not denied:
            auth.update(authenticated=True, hint=None)
        info["warnings"].append(str(exc))
    return info


def _changes(client: ForgejoClient, ref: PullRequestRef) -> tuple[list[dict[str, Any]], bool]:
    files, partial = client.pages(*_repo(ref), "pulls", ref.number, "files")
    if any(not isinstance(item.get("filename"), str) for item in files):
        raise ValueError("Forgejo returned an invalid changed-file response.")
    return files, partial


def _previous_path(change: dict[str, Any]) -> str:
    previous = change.get("previous_filename")
    return previous if isinstance(previous, str) and previous else change["filename"]


def _line_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content(client: ForgejoClient, ref: PullRequestRef, path: str, revision: str) -> str:
    content = client.raw(*_repo(ref), "raw", *path.split("/"), ref=revision)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Expanded context is unavailable for this binary or invalid file."
        ) from exc


def _check_relative(*paths: str | None) -> None:
    for value in paths:
        if value is not None and (
            not value
            or value.startswith("/")
            or any(part in {".", ".."} for part in value.split("/"))
            or "\0" in value
        ):
            raise ValueError("Invalid repository-relative file path.")


def _unsupported(action: str) -> ValueError:
    return ValueError(f"{action} Forgejo pull requests from Omnigent is not supported yet.")


class ForgejoPullRequests:
    """Normalize Forgejo pull requests, comments, statuses, and files to the shared contract."""

    capabilities = _CAPABILITIES
    workspace_info = staticmethod(_info)
    reference_info = staticmethod(_info)
    shell_pr_operations = staticmethod(forgejo_observer.shell_pr_operations)
    pr_from_object = staticmethod(forgejo_observer.pr_from_object)
    mcp_prs = staticmethod(forgejo_observer.mcp_prs)

    def titles_available(self, root: str) -> bool:
        del root
        return True

    def pr_title(
        self, root: str, reference: PullRequestRef, deadline: float
    ) -> tuple[str | None, bool]:
        from omnigent.runner.forgejo_client import ForgejoTimeoutError

        try:
            with _resolved(root, reference, deadline) as resolved:
                title = resolved[2].get("title") if resolved else None
            return (title.strip() or None if isinstance(title, str) else None), False
        except ForgejoTimeoutError:
            return None, True
        except ValueError:
            return None, False

    def verify_accessible(self, root: str, reference: PullRequestRef) -> None:
        with _resolved(root, reference):
            pass

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        pass

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        with _resolved(root, reference) as resolved:
            if resolved is None:
                return {"object": "list", "data": [], "has_more": False}
            changes, partial = _changes(resolved[0], resolved[1])
        data = [
            {
                "object": CHANGED_FILE_OBJECT,
                "path": change["filename"],
                "name": change["filename"],
                "previous_path": _previous_path(change)
                if change.get("status") == "renamed"
                else None,
                "status": _FILE_STATUSES.get(str(change.get("status")), "modified"),
                "lines_added": _line_count(change.get("additions")),
                "lines_removed": _line_count(change.get("deletions")),
            }
            for change in changes
        ]
        return {
            "object": "list",
            "data": data,
            "has_more": partial,
            "warning": "Forgejo returned incomplete file changes." if partial else None,
        }

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        with _resolved(root, reference) as resolved:
            if resolved is None:
                return {"object": PR_DIFF_OBJECT, "patch": ""}
            client, ref, _ = resolved
            patch = client.text(*_repo(ref), "pulls", f"{ref.number}.diff")
        return {"object": PR_DIFF_OBJECT, "patch": patch}

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
        _check_relative(path, previous_path)
        with _resolved(root, reference) as resolved:
            if resolved is None:
                if not base:
                    raise ValueError("No pull request is available for expanded context.")

                def run(args: list[str]) -> tuple[int | None, str]:
                    return _git(root, args)

                revision = local_git.resolve_diff_base(run, base)
                return {
                    "object": FILE_DIFF_OBJECT,
                    "path": path,
                    "before": local_git.read_file(run, revision, previous_path or path),
                    "after": local_git.read_file(run, "HEAD", path),
                }
            client, ref, pull = resolved
            head, merge_base = _object(pull.get("head")).get("sha"), pull.get("merge_base")
            if not (
                isinstance(head, str)
                and isinstance(merge_base, str)
                and _COMMIT.fullmatch(head)
                and _COMMIT.fullmatch(merge_base)
            ):
                raise ValueError("Forgejo has not prepared the pull request's revisions. Refresh.")
            if (head_sha is not None and head_sha != head) or (
                base_sha is not None and base_sha != merge_base
            ):
                raise ValueError(
                    "The pull request changed. Refresh before expanding file context."
                )
            changes, _ = _changes(client, ref)
            change = next((c for c in changes if c["filename"] == path), None)
            old_path = _previous_path(change) if change else ""
            if change is None or (previous_path is not None and previous_path != old_path):
                raise ValueError(
                    "This file is not in the current pull request diff. Refresh to retry."
                )
            _check_relative(old_path)
            status = _FILE_STATUSES.get(str(change.get("status")), "modified")
            before = None if status == "created" else _content(client, ref, old_path, merge_base)
            after = None if status == "deleted" else _content(client, ref, path, head)
        return {"object": FILE_DIFF_OBJECT, "path": path, "before": before, "after": after}

    def set_preference(
        self,
        root: str,
        reference: PullRequestRef | None,
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        del root, reference
        if account is not None or remote is not None:
            raise ValueError("Use fj or tea on the host to select a Forgejo login or remote.")

    def repo_labels(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        del root, reference
        raise _unsupported("Editing labels on")

    def edit_labels(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        add: Sequence[str],
        remove: Sequence[str],
    ) -> dict[str, Any]:
        del root, reference, add, remove
        raise _unsupported("Editing labels on")

    def merge_options(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        del root, reference
        raise _unsupported("Merging")

    def merge(
        self, root: str, reference: PullRequestRef, *, merge_method: str, expected_head: str
    ) -> dict[str, Any]:
        del root, reference, merge_method, expected_head
        raise _unsupported("Merging")

    def disable_auto_merge(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        del root, reference
        raise _unsupported("Merging")

    def review_candidates(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        del root, reference
        raise _unsupported("Editing reviewers on")

    def edit_reviewers(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        add: Sequence[ReviewerRef],
        remove: Sequence[ReviewerRef],
    ) -> dict[str, Any]:
        del root, reference, add, remove
        raise _unsupported("Editing reviewers on")

    def create_comment(self, root: str, reference: PullRequestRef, *, body: str) -> dict[str, Any]:
        """Reject comment posting for this provider."""
        del root, reference, body
        raise ValueError("Posting comments for this provider is not supported yet.")

    def edit_details(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        title: str | None,
        body: str | None,
    ) -> dict[str, Any]:
        del root, reference, title, body
        raise _unsupported("Editing descriptions of")

    def upload_attachment(
        self,
        root: str,
        reference: PullRequestRef,
        *,
        name: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        del root, reference, name, content_type, content
        raise _unsupported("Attaching files to")


PULL_REQUESTS: PullRequestFacet = ForgejoPullRequests()
