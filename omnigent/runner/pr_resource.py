"""Provider-neutral orchestration of the session pull request panel.

Decides which pull request a request is about (the selected or first tracked
PR, else the PR of the workspace's branch) and which git provider serves it,
and keeps the session's tracked PRs and their cached titles. Every step that
talks to a forge goes through that provider's
:class:`~omnigent.runner.git_providers.PullRequestFacet`.
"""

from __future__ import annotations

import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from filelock import Timeout as FileLockTimeout

from omnigent.git_providers import host_of, load_facet, provider, providers, resolve_remote
from omnigent.runner.git_providers import (
    PR_DIFF_OBJECT,
    ProviderCapabilities,
    PullRequestFacet,
    local_git,
    unsupported_remote_info,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, SessionPullRequest
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# Cache failed lookups too so inaccessible PRs do not add work to every poll.
_PR_TITLE_CACHE_SECONDS = 300.0
_PR_TITLE_TIMEOUT_RETRY_SECONDS = 15.0
_PR_TITLE_LOOKUP_SECONDS = 2.0
# Leave headroom for persistence and the runner proxy's ten-second response limit.
_PR_TITLE_REQUEST_SECONDS = 8.0
# A git config key that pins a workspace to one provider id.
_PROVIDER_CONFIG_KEY = "omnigent.gitprovider"


@dataclass(frozen=True)
class ProviderResolution:
    """The git provider that serves a request.

    :ivar provider: The provider id, or ``None`` when no provider is registered.
        A tracked PR or the ``omnigent.gitprovider`` setting can name a provider
        without a pull request facet.
    :ivar remote_host: The tracked PR's host, else the host of the workspace's
        first network remote; the unsupported-remote payload names it.
    :ivar unclaimed: The workspace has a network remote, but no provider with a
        pull request facet claims one, so the first registered provider serves it.
    """

    provider: str | None
    remote_host: str | None = None
    unclaimed: bool = False


def _git_output(argv: list[str], *, cwd: str) -> tuple[int | None, str]:
    """Run a read-only ``git`` command; the return code is ``None`` when git cannot run."""
    try:
        result = subprocess.run(
            ["git", *argv], cwd=cwd, capture_output=True, timeout=_git_timeout_seconds()
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _remote_urls(root: str) -> list[str]:
    """Return each remote's first configured URL, ``origin`` first, then in config order."""
    remotes = local_git.remote_urls(lambda argv: _git_output(argv, cwd=root))
    return [url for _, url in remotes]


def _configured_provider(root: str) -> str | None:
    """Return the repository's ``omnigent.gitprovider`` value, lower-cased, if set.

    Global and system config are ignored, so one setting cannot re-route every workspace.
    """
    rc, out = _git_output(["config", "--local", "--get", _PROVIDER_CONFIG_KEY], cwd=root)
    return (out.strip().lower() or None) if rc == 0 else None


def _workspace_provider(root: str) -> ProviderResolution:
    """Resolve the provider of the workspace itself, ignoring tracked PRs."""
    urls = _remote_urls(root)
    first_host = next((host for url in urls if (host := host_of(url))), None)
    configured = _configured_provider(root)
    if configured:
        return ProviderResolution(configured, first_host)
    for url in urls:
        parsed = resolve_remote(url)
        if parsed is not None and _facet(parsed.provider) is not None:
            return ProviderResolution(parsed.provider, first_host)
    # No provider with a facet claims a remote, e.g. an ssh host alias; the first
    # provider's CLI may still resolve it.
    fallback = next(iter(providers()), None)
    return ProviderResolution(
        fallback.id if fallback is not None else None,
        first_host,
        unclaimed=first_host is not None,
    )


def _resolve(root: str, reference: PullRequestRef | None) -> ProviderResolution:
    """Resolve ``reference``'s provider, else the workspace's own."""
    if reference is not None:
        return ProviderResolution(reference.provider, reference.host)
    return _workspace_provider(root)


def resolve_provider(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> ProviderResolution:
    """Return the git provider that serves a panel request.

    In order: the selected or first tracked PR's provider; the repository's own
    ``omnigent.gitprovider`` git config value; the first remote (``origin``,
    then the others in config order) claimed by a provider with a pull request
    facet. Otherwise the first registered provider serves the workspace, and
    the resolution is ``unclaimed`` when the workspace has a network remote.

    :param root: Absolute workspace path.
    :param session_id: Session whose tracked PRs come first, if any.
    :param pr_url: The selected PR, which must be tracked by the session.
    :raises ValueError: If ``pr_url`` is not associated with the session.
    """
    return _resolve(root, _default_pr(session_id, pr_url))


def _facet(provider_id: str | None) -> PullRequestFacet | None:
    """Return the provider's pull request facet, or ``None`` when it has none."""
    return load_facet(provider_id, "pull_requests") if provider_id else None


def _serving_facet(root: str, reference: PullRequestRef | None) -> PullRequestFacet | None:
    """Return the facet for ``reference``, else for the workspace's own provider."""
    return _facet(_resolve(root, reference).provider)


def _selected_pr(session_id: str, pr_url: str) -> SessionPullRequest:
    reference = PullRequestRef.from_url(pr_url)
    for entry in SessionPrRegistry(session_id).list():
        if entry.url == reference.url:
            return entry
    raise ValueError("This pull request is not associated with the session")


def _default_pr(session_id: str | None, pr_url: str | None) -> SessionPullRequest | None:
    if session_id is None:
        return None
    if pr_url:
        return _selected_pr(session_id, pr_url)
    entries = SessionPrRegistry(session_id).list()
    return entries[0] if entries else None


def _cannot_serve(info: dict[str, Any], capabilities: ProviderCapabilities) -> bool:
    """Whether a provider's workspace info shows that it cannot serve the workspace.

    True when its CLI, if it has one, is present and signed in, yet no
    repository resolved and the panel offers no other account to try.
    """
    auth = info.get("auth")
    if info.get("available") is not True or not isinstance(auth, dict):
        return False
    cli = auth.get("cli")
    if not auth.get("authenticated") or (isinstance(cli, dict) and not cli.get("available")):
        # The panel shows the provider's install or sign-in guidance instead.
        return False
    repo = info.get("repo")
    if info.get("pr") is not None or (isinstance(repo, dict) and repo.get("name_with_owner")):
        return False
    accounts = auth.get("accounts")
    several_accounts = isinstance(accounts, list) and len(accounts) > 1
    return not (capabilities.account_switching and several_accounts)


def _workspace_info(root: str) -> dict[str, Any]:
    """Info for the workspace's branch, or the unsupported-remote payload."""
    resolution = _workspace_provider(root)
    facet = _facet(resolution.provider)
    if facet is None:
        return unsupported_remote_info(resolution.remote_host or "")
    info = facet.workspace_info(root)
    if resolution.unclaimed and _cannot_serve(info, facet.capabilities):
        return unsupported_remote_info(resolution.remote_host or "")
    return info


def _reference_info(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Info for one tracked PR, or the unsupported payload when its provider has no facet."""
    facet = _facet(reference.provider)
    if facet is None:
        return {**unsupported_remote_info(reference.host), "selected_pr_url": reference.url}
    return facet.reference_info(root, reference)


def _pr_title(pr: object) -> str | None:
    """Return the stripped title of an info payload's ``pr``, or ``None``."""
    title = pr.get("title") if isinstance(pr, dict) else None
    if not isinstance(title, str):
        return None
    return title.strip() or None


def _title_facets(root: str, entries: list[SessionPullRequest]) -> dict[str, PullRequestFacet]:
    """Map each tracked provider whose titles can be looked up now to its facet."""
    facets: dict[str, PullRequestFacet] = {}
    for provider_id in dict.fromkeys(entry.provider for entry in entries):
        facet = _facet(provider_id)
        if facet is not None and facet.titles_available(root):
            facets[provider_id] = facet
    return facets


def _session_prs_with_titles(
    root: str,
    info: dict[str, Any],
    registry: SessionPrRegistry,
    entries: list[SessionPullRequest],
    request_deadline: float,
) -> list[dict[str, Any]]:
    facets = _title_facets(root, entries)
    now = time.time()
    titles: dict[str, str | None] = {}
    timed_out_urls: set[str] = set()
    pending: list[SessionPullRequest] = []
    for entry in entries:
        if entry.provider not in facets:
            continue
        cache_seconds = (
            _PR_TITLE_TIMEOUT_RETRY_SECONDS
            if entry.title_lookup_timed_out
            else _PR_TITLE_CACHE_SECONDS
        )
        stale = now - entry.title_checked_at >= cache_seconds
        if entry.url == info.get("selected_pr_url"):
            title = _pr_title(info.get("pr"))
            if stale or (title is not None and title != entry.title):
                titles[entry.url] = title
        elif stale:
            pending.append(entry)

    # Short-backoff retries must not starve PRs that have waited longer or never ran.
    pending.sort(key=lambda entry: entry.title_checked_at)
    deadline = min(request_deadline, time.monotonic() + _PR_TITLE_LOOKUP_SECONDS)

    def fetch_title(entry: SessionPullRequest) -> tuple[str, str | None, bool] | None:
        if time.monotonic() >= deadline:
            return None
        title, timed_out = facets[entry.provider].pr_title(root, entry, deadline)
        return entry.url, title, timed_out

    may_cache = time.monotonic() < request_deadline
    if pending and time.monotonic() < deadline:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
            for result in executor.map(fetch_title, pending):
                if result is not None:
                    url, title, timed_out = result
                    titles[url] = title
                    if timed_out:
                        timed_out_urls.add(url)
    if titles and may_cache:
        try:
            registry.update_titles(titles, timestamp=now, timed_out_urls=timed_out_urls)
        except (OSError, ValueError, FileLockTimeout):
            _logger.debug("Could not cache session PR titles", exc_info=True)

    return [
        {**entry.model_dump(), "title": titles.get(entry.url) or entry.title} for entry in entries
    ]


def pr_info(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Read the selected session PR, with branch inference for untracked sessions.

    :param root: Absolute workspace path.
    :param session_id: Session whose tracked PRs to list; ``None`` reads only
        the workspace branch's PR.
    :param pr_url: The selected PR, which must be tracked by the session.
    :returns: A ``session.github.info`` payload.
    """
    if session_id is None:
        return _workspace_info(root)
    request_deadline = time.monotonic() + _PR_TITLE_REQUEST_SECONDS
    registry = SessionPrRegistry(session_id)
    entries = registry.list()
    if pr_url:
        info = _reference_info(root, _selected_pr(session_id, pr_url))
    elif entries:
        info = _reference_info(root, entries[0])
    else:
        info = _workspace_info(root)
        pr = info.get("pr")
        if isinstance(pr, dict) and isinstance(pr.get("url"), str):
            reference = PullRequestRef.from_url(pr["url"])
            registry.record([reference], relationship="inferred", source="branch")
            entries = registry.list()
            if any(entry.url == reference.url for entry in entries):
                facet = _facet(reference.provider)
                if facet is not None:
                    facet.on_inferred_pr(root, reference)
                info["selected_pr_url"] = reference.url
            else:
                info["pr"] = None
    info["prs"] = _session_prs_with_titles(root, info, registry, entries, request_deadline)
    info["tracking_available"] = True
    return info


def update_session_pr(root: str, session_id: str, url: str, action: str) -> dict[str, Any]:
    """Attach a PR the host can read, or remember that the user removed one.

    :raises ValueError: If the provider cannot read the PR, has no pull request
        support, the registry is busy, or ``action`` is unknown.
    """
    reference = PullRequestRef.from_url(url)
    registry = SessionPrRegistry(session_id)
    try:
        if action == "attach":
            facet = _facet(reference.provider)
            if facet is None:
                descriptor = provider(reference.provider)
                name = descriptor.display_name if descriptor else reference.provider
                raise ValueError(f"{name} pull requests are not supported")
            facet.verify_accessible(root, reference)
            registry.record([reference], relationship="attached", source="user")
            return pr_info(root, session_id=session_id, pr_url=reference.url)
        if action == "remove":
            registry.remove(reference.url)
            return pr_info(root, session_id=session_id)
    except FileLockTimeout as exc:
        raise ValueError("PR tracking is busy; try again.") from exc
    raise ValueError("Expected attach or remove")


def set_pr_preference(
    root: str,
    *,
    account: str | None = None,
    remote: str | None = None,
    session_id: str | None = None,
    pr_url: str | None = None,
) -> dict[str, Any]:
    """Apply an account and/or base remote choice, then return refreshed info.

    With ``pr_url`` the account applies to that PR; otherwise both apply to the
    workspace. A choice goes to the provider only when its capabilities include
    it, so an unsupported choice changes nothing.

    :param account: Account to prefer, ``None`` to leave it, or empty to clear it.
    :param remote: Base remote to set, or ``None`` to leave the base unchanged.
    :returns: The refreshed :func:`pr_info` payload.
    """
    reference = _selected_pr(session_id, pr_url) if pr_url and session_id else None
    facet = _serving_facet(root, reference)
    if facet is not None:
        capabilities = facet.capabilities
        account = account if capabilities.account_switching else None
        remote = remote if capabilities.base_remote_selection else None
        if account is not None or remote:
            facet.set_preference(root, reference, account=account, remote=remote)
    if reference is not None:
        return pr_info(root, session_id=session_id, pr_url=pr_url)
    return pr_info(root, session_id=session_id)


def pr_changed_files(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """List the selected PR's changed files; empty when no provider serves it."""
    reference = _default_pr(session_id, pr_url)
    facet = _serving_facet(root, reference)
    if facet is None:
        return {"object": "list", "data": [], "has_more": False}
    return facet.changed_files(root, reference)


def pr_diff(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the selected PR as one unified diff; empty when no provider serves it."""
    reference = _default_pr(session_id, pr_url)
    facet = _serving_facet(root, reference)
    if facet is None:
        return {"object": PR_DIFF_OBJECT, "patch": ""}
    return facet.pr_diff(root, reference)


def pr_file_diff(
    root: str,
    base: str,
    path: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    previous_path: str | None = None,
    head_sha: str | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Return one file's before and after content for the selected PR.

    :param base: Base branch for a workspace without a tracked PR; empty uses
        the branch PR's base.
    :raises ValueError: When no provider serves the workspace.
    """
    reference = _default_pr(session_id, pr_url)
    facet = _serving_facet(root, reference)
    if facet is None:
        raise ValueError("No supported git provider serves this workspace")
    return facet.file_diff(
        root,
        reference,
        path,
        base=base,
        previous_path=previous_path,
        head_sha=head_sha,
        base_sha=base_sha,
    )
