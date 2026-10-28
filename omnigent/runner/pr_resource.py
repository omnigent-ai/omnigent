"""Provider-neutral orchestration of the session pull request panel.

Decides which pull request a request is about (the selected or first tracked
PR, else the PR of the workspace's branch) and which git provider serves it,
and keeps the session's tracked PRs and their cached titles. Every step that
talks to a forge goes through that provider's
:class:`~omnigent.runner.git_providers.PullRequestFacet`.

Servers that track a session's PRs vouch for the PR a request targets
(:func:`server_tracking`), so it is served even if this host never recorded it.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any

from filelock import Timeout as FileLockTimeout

from omnigent.git_providers import (
    host_of,
    load_facet,
    provider,
    provider_display,
    providers,
    resolve_remote,
)
from omnigent.runner import pr_reports
from omnigent.runner.git_providers import (
    PR_DIFF_OBJECT,
    ProviderCapabilities,
    PullRequestFacet,
    ReviewerRef,
    local_git,
    unsupported_remote_info,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, SessionPullRequest
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# Whether the server vouches for the request's PR, and its first PR for requests that select none.
_SERVER_TRACKING: ContextVar[tuple[bool, str | None]] = ContextVar(
    "server_tracked_pull_request", default=(False, None)
)

# Cache failed lookups too so inaccessible PRs do not add work to every poll.
_PR_TITLE_CACHE_SECONDS = 300.0
_PR_TITLE_TIMEOUT_RETRY_SECONDS = 15.0
_PR_TITLE_LOOKUP_SECONDS = 2.0
# Leave headroom for persistence and the runner proxy's ten-second response limit.
_PR_TITLE_REQUEST_SECONDS = 8.0
# A git config key that pins a workspace to one provider id.
_PROVIDER_CONFIG_KEY = "omnigent.gitprovider"
# GitHub allows at most 100 labels on a pull request, so no larger edit can apply.
_MAX_LABEL_EDIT = 100
_MERGE_METHODS = frozenset({"merge", "squash", "rebase"})
# A full SHA-1 or SHA-256 commit id; ``gh`` would reject an abbreviation.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# Bounds one reviewer edit's request body; the panel sends one reviewer at a time.
_MAX_REVIEWER_EDIT = 100
# GitHub's limits on a pull request's title and description.
_MAX_TITLE_CHARS = 256
_MAX_BODY_CHARS = 65536
# GitHub's image limit. Videos may be 100 MB there, but the server and host
# tunnels each carry the file base64-encoded (a third larger) in one 100 MB frame.
_MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_VIDEO_ATTACHMENT_BYTES = 70 * 1024 * 1024
# Leading bytes of each media type GitHub accepts as an attachment, with its extension.
_ATTACHMENT_SIGNATURES: tuple[tuple[int, bytes, str, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (0, b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (0, b"GIF87a", "image/gif", "gif"),
    (0, b"GIF89a", "image/gif", "gif"),
    (0, b"\x1a\x45\xdf\xa3", "video/webm", "webm"),
)
# ISO media files (``ftyp`` at byte 4) name their format in the next four bytes;
# only video brands count, so HEIC and AVIF photos aren't sent as MP4.
_QUICKTIME_BRAND = b"qt  "
_MP4_BRANDS = frozenset(
    (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V ")
)

_DISCOVERY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pr-discovery")
_DISCOVERY_LOCK = Lock()
_DISCOVERY_JOBS: dict[tuple[str, str, str | None], Future[dict[str, Any]]] = {}
_DISCOVERY_RESULT_LIMIT = 128


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


def _workspace_providers(root: str) -> list[ProviderResolution]:
    """Find every provider in the checkout, retaining the preferred display order."""
    urls = _remote_urls(root)
    first_host = next((host for url in urls if (host := host_of(url))), None)
    configured = _configured_provider(root)
    found = {configured: ProviderResolution(configured, first_host)} if configured else {}
    for url in urls:
        parsed = resolve_remote(url)
        if parsed is not None and parsed.provider not in found:
            try:
                facet = _facet(parsed.provider)
            except Exception:  # optional integrations must not hide other remotes
                _logger.warning("Could not load git provider %s", parsed.provider, exc_info=True)
                continue
            if facet is not None:
                found[parsed.provider] = ProviderResolution(parsed.provider, first_host)
    if found:
        return list(found.values())
    # No provider with a facet claims a remote, e.g. an ssh host alias; the first
    # provider's CLI may still resolve it.
    fallback = next(iter(providers()), None)
    return [
        ProviderResolution(
            fallback.id if fallback is not None else None,
            first_host,
            unclaimed=first_host is not None,
        )
    ]


def _workspace_provider(root: str) -> ProviderResolution:
    """Choose the default display provider; discovery still visits every provider."""
    return _workspace_providers(root)[0]


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


@contextmanager
def server_tracking(server_tracked: object, default_pr_url: object = None) -> Iterator[None]:
    """Serve the PR the server tracks for the session, even if this host never recorded it.

    :param server_tracked: The request's ``server_tracked`` value; servers that
        track the session's PRs send ``True`` (``"1"`` in a query string).
    :param default_pr_url: The server's first PR, served when the request selects none.
    """
    tracked = server_tracked is True or server_tracked in ("1", "true")
    default = default_pr_url if tracked and isinstance(default_pr_url, str) else None
    token = _SERVER_TRACKING.set((tracked, default or None))
    try:
        yield
    finally:
        _SERVER_TRACKING.reset(token)


def _selected_pr(session_id: str, pr_url: str) -> SessionPullRequest:
    reference = PullRequestRef.from_url(pr_url)
    tracked, _ = _SERVER_TRACKING.get()
    try:
        entries = SessionPrRegistry(session_id).list()
    except ValueError:
        if not tracked:
            raise
        entries = []
    for entry in entries:
        if entry.url == reference.url:
            return entry
    if tracked:
        now = time.time()
        return SessionPullRequest(
            **reference.model_dump(),
            relationship="attached",
            source="server",
            first_seen_at=now,
            last_seen_at=now,
        )
    raise ValueError("This pull request is not associated with the session")


def _default_pr(session_id: str | None, pr_url: str | None) -> SessionPullRequest | None:
    if session_id is None:
        return None
    pr_url = pr_url or _SERVER_TRACKING.get()[1]
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


def _workspace_info(root: str, resolution: ProviderResolution | None = None) -> dict[str, Any]:
    """Info for the workspace's branch, or the unsupported-remote payload."""
    resolution = resolution or _workspace_provider(root)
    facet = _facet(resolution.provider)
    if facet is None:
        return unsupported_remote_info(resolution.remote_host or "")
    info = facet.workspace_info(root)
    if resolution.unclaimed and _cannot_serve(info, facet.capabilities):
        return unsupported_remote_info(resolution.remote_host or "")
    return info


def _reference_info(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Info for one tracked PR, or the unsupported payload when its provider has no facet."""
    try:
        facet = _facet(reference.provider)
        if facet is None:
            return {**unsupported_remote_info(reference.host), "selected_pr_url": reference.url}
        return facet.reference_info(root, reference)
    except Exception:  # retain the selector and healthy providers on failure
        _logger.warning("Git provider %s failed PR lookup", reference.provider, exc_info=True)
        return {
            **unsupported_remote_info(reference.host),
            "reason": "provider_unavailable",
            "provider": reference.provider,
            "selected_pr_url": reference.url,
            "warnings": ["Could not load this pull request. Refresh to retry."],
        }


def _discovery_info(root: str, resolution: ProviderResolution) -> dict[str, Any]:
    try:
        return _workspace_info(root, resolution)
    except Exception:  # one unavailable forge must not hide the others
        _logger.warning("Git provider %s failed discovery", resolution.provider, exc_info=True)
        return {
            **unsupported_remote_info(resolution.remote_host or ""),
            "provider": resolution.provider,
            "warnings": [
                f"{resolution.provider} pull request discovery failed. Refresh to retry."
            ],
        }


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
        try:
            facet = _facet(provider_id)
            if facet is not None and facet.titles_available(root):
                facets[provider_id] = facet
        except Exception:  # optional titles must not hide the selected PR
            _logger.warning(
                "Git provider %s could not prepare PR title lookup", provider_id, exc_info=True
            )
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
        try:
            title, timed_out = facets[entry.provider].pr_title(root, entry, deadline)
        except Exception:  # keep the existing cache when an optional lookup fails
            _logger.warning("Git provider %s failed in pr_title", entry.provider, exc_info=True)
            return None
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
        {
            **entry.model_dump(),
            "title": titles.get(entry.url) or entry.title,
            "provider_display": provider_display(entry.provider),
        }
        for entry in entries
    ]


def _associate_discovered_pr(
    root: str, registry: SessionPrRegistry, candidate: dict[str, Any], timestamp: float
) -> None:
    pr = candidate.get("pr")
    if isinstance(pr, dict) and isinstance(pr.get("url"), str):
        try:
            inferred = PullRequestRef.from_url(pr["url"])
            if inferred.provider != candidate.get("provider"):
                raise ValueError("Discovery returned a different provider's pull request")
            registry.record(
                [inferred], relationship="inferred", source="branch", timestamp=timestamp
            )
            pr_reports.notify(registry.session_id)
            if any(entry.url == inferred.url for entry in registry.list()):
                candidate["selected_pr_url"] = inferred.url
                try:
                    facet = _facet(inferred.provider)
                    if facet is not None:
                        facet.on_inferred_pr(root, inferred)
                    title = _pr_title(pr)
                    if title is not None:
                        registry.update_titles({inferred.url: title})
                except Exception:  # metadata cannot undo a valid association
                    _logger.warning("Could not cache discovered PR metadata", exc_info=True)
            else:
                candidate["pr"] = None
                candidate.pop("selected_pr_url", None)
        except Exception:  # failed optional inference preserves selected PRs
            _logger.warning("Could not associate discovered pull request", exc_info=True)
            candidate["pr"] = None
            candidate.pop("selected_pr_url", None)
            candidate.setdefault("warnings", []).append("Could not associate this pull request.")


def _background_discovery(
    root: str, session_id: str, resolution: ProviderResolution
) -> Future[dict[str, Any]]:
    """Share discovery across polls until a request collects its result."""
    key = (root, session_id, resolution.provider)

    def discover() -> dict[str, Any]:
        candidate = _discovery_info(root, resolution)
        _associate_discovered_pr(root, SessionPrRegistry(session_id), candidate, time.time())
        return candidate

    with _DISCOVERY_LOCK:
        existing = _DISCOVERY_JOBS.get(key)
        if existing is not None:
            return existing
        # Bound completed results for sessions that are no longer polled.
        for stale_key in list(_DISCOVERY_JOBS):
            if len(_DISCOVERY_JOBS) < _DISCOVERY_RESULT_LIMIT:
                break
            if _DISCOVERY_JOBS[stale_key].done():
                del _DISCOVERY_JOBS[stale_key]
        future = _DISCOVERY_POOL.submit(discover)
        _DISCOVERY_JOBS[key] = future
    return future


def pr_info(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Read the selected session PR and discover untracked providers from all remotes.

    :param root: Absolute workspace path.
    :param session_id: Session whose tracked PRs to list; ``None`` reads only
        the workspace branch's PR.
    :param pr_url: The selected PR, which must be tracked by the session.
    :returns: A ``session.github.info`` payload.
    """
    if session_id is None:
        info = _workspace_info(root)
        provider_id = info.get("provider")
        info["provider_display"] = (
            provider_display(provider_id) if isinstance(provider_id, str) else None
        )
        return info
    request_deadline = time.monotonic() + _PR_TITLE_REQUEST_SECONDS
    registry = SessionPrRegistry(session_id)
    entries = registry.list()
    default_url = pr_url or _SERVER_TRACKING.get()[1]
    reference = (
        _selected_pr(session_id, default_url) if default_url else entries[0] if entries else None
    )
    tracked_providers = {entry.provider for entry in entries}
    if reference is not None:
        tracked_providers.add(reference.provider)
    resolutions = (
        []
        if pr_url
        else [
            resolution
            for resolution in _workspace_providers(root)
            if resolution.provider not in tracked_providers
        ]
    )
    discovered: list[dict[str, Any]] = []
    if not resolutions:
        assert reference is not None
        info = _reference_info(root, reference)
    elif reference is not None:
        pending = [
            (value, _background_discovery(root, session_id, value)) for value in resolutions
        ]
        info = _reference_info(root, reference)
        # Collect quick results; slow forges will appear on a subsequent poll.
        deadline = time.monotonic() + 0.1
        for resolution, future in pending:
            with suppress(FutureTimeout):
                discovered.append(future.result(timeout=max(0, deadline - time.monotonic())))
                with _DISCOVERY_LOCK:
                    key = (root, session_id, resolution.provider)
                    if _DISCOVERY_JOBS.get(key) is future:
                        del _DISCOVERY_JOBS[key]
    elif len(resolutions) == 1:
        discovered = [_discovery_info(root, resolutions[0])]
        info = discovered[0]
    else:
        # Each provider enforces its request budget; unrelated forges run concurrently.
        with ThreadPoolExecutor(max_workers=min(4, len(resolutions))) as pool:
            discovered = list(pool.map(lambda value: _discovery_info(root, value), resolutions))
            info = discovered[0]
    if reference is None:
        discovered_at = time.time()
        for candidate in discovered:
            _associate_discovered_pr(root, registry, candidate, discovered_at)
    entries = registry.list()
    if reference is None:
        info = next((candidate for candidate in discovered if candidate.get("pr")), info)
    for candidate in discovered:
        if candidate is not info:
            info.setdefault("discovery_warnings", []).extend(candidate.get("warnings", []))
    info["prs"] = _session_prs_with_titles(root, info, registry, entries, request_deadline)
    info["tracking_available"] = True
    provider_id = info.get("provider")
    info["provider_display"] = (
        provider_display(provider_id) if isinstance(provider_id, str) else None
    )
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


def _capable_target(
    session_id: str | None,
    pr_url: str | None,
    *,
    capable: Callable[[ProviderCapabilities], bool],
    unselected: str,
    unsupported: str,
) -> tuple[PullRequestFacet, SessionPullRequest]:
    """Return the selected tracked PR and its facet, which must pass ``capable``.

    :param unselected: Message when no PR is selected.
    :param unsupported: Message with a ``{kind}`` placeholder, such as
        ``"GitLab merge request"``, when the provider lacks the capability.
    """
    if not session_id or not pr_url:
        raise ValueError(unselected)
    reference = _selected_pr(session_id, pr_url)
    facet = _facet(reference.provider)
    if facet is None or not capable(facet.capabilities):
        display = provider_display(reference.provider)
        kind = (
            f"{display['display_name']} {display['request_name']}"
            if display
            else f"{reference.provider} pull request"
        )
        raise ValueError(unsupported.format(kind=kind))
    return facet, reference


def _label_target(
    session_id: str | None, pr_url: str | None
) -> tuple[PullRequestFacet, SessionPullRequest]:
    """Return the selected tracked PR and its facet, which must support label editing."""
    return _capable_target(
        session_id,
        pr_url,
        capable=lambda capabilities: capabilities.label_editing,
        unselected="Select a pull request to edit its labels",
        unsupported="Editing {kind} labels is not supported",
    )


def _reviewer_target(
    session_id: str | None, pr_url: str | None
) -> tuple[PullRequestFacet, SessionPullRequest]:
    """Return the selected tracked PR and its facet, which must support reviewer editing."""
    return _capable_target(
        session_id,
        pr_url,
        capable=lambda capabilities: capabilities.reviewer_editing,
        unselected="Select a pull request to edit its reviewers",
        unsupported="Editing {kind} reviewers is not supported",
    )


def _merge_target(
    session_id: str | None, pr_url: str | None
) -> tuple[PullRequestFacet, SessionPullRequest]:
    """Return the selected tracked PR and its facet, which must support merging."""
    return _capable_target(
        session_id,
        pr_url,
        capable=lambda capabilities: capabilities.merging,
        unselected="Select a pull request to merge",
        unsupported="Merging a {kind} is not supported",
    )


def _label_names(value: object, field: str) -> list[str]:
    """Return a request field's label names, without case-insensitive repeats, in order.

    GitHub label names ignore case, so the first spelling sent is kept.
    """
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(name, str) and name.strip() for name in value
    ):
        raise ValueError(f"Expected {field} to be a list of label names")
    unique: dict[str, str] = {}
    for name in value:
        unique.setdefault(name.casefold(), name)
    return list(unique.values())


def pr_labels(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """List the labels the selected PR's repository offers.

    :raises ValueError: Without a selected tracked PR, or when its provider
        cannot edit labels.
    """
    facet, reference = _label_target(session_id, pr_url)
    return facet.repo_labels(root, reference)


def update_pr_labels(
    root: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    add: object = (),
    remove: object = (),
) -> dict[str, Any]:
    """Add and remove labels on the selected PR.

    :param add: Names of existing repository labels to add.
    :param remove: Names of labels to remove; one the PR lacks is ignored.
    :returns: A ``session.github.pr_labels`` with the PR's resulting labels.
    :raises ValueError: On an empty, overlapping, or oversized edit, a removal
        named ``.`` or ``..``, without a selected tracked PR, or when the
        provider refuses the change.
    """
    add_names = _label_names(add, "add")
    remove_names = _label_names(remove, "remove")
    if not add_names and not remove_names:
        raise ValueError("Expected labels to add or remove")
    if {name.casefold() for name in add_names} & {name.casefold() for name in remove_names}:
        raise ValueError("A label cannot be both added and removed")
    # A removal names the label in the URL path, where these are dot segments.
    if {".", ".."} & set(remove_names):
        raise ValueError("Remove labels named . or .. on GitHub")
    if len(add_names) + len(remove_names) > _MAX_LABEL_EDIT:
        raise ValueError(f"Edit at most {_MAX_LABEL_EDIT} labels at once")
    facet, reference = _label_target(session_id, pr_url)
    return facet.edit_labels(root, reference, add=add_names, remove=remove_names)


def pr_merge_options(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the merge methods the selected PR's repository allows.

    :raises ValueError: Without a selected tracked PR, or when its provider cannot merge.
    """
    facet, reference = _merge_target(session_id, pr_url)
    return facet.merge_options(root, reference)


def merge_pr(
    root: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    merge_method: object = None,
    expected_head: object = None,
) -> dict[str, Any]:
    """Merge the selected PR, only while its head is still ``expected_head``.

    :param merge_method: ``merge``, ``squash``, or ``rebase``.
    :param expected_head: The full head commit SHA the user reviewed.
    :returns: A ``session.github.pr_merge``.
    :raises ValueError: On an unknown method or malformed SHA, without a
        selected tracked PR, or when the provider refuses the merge.
    """
    if not isinstance(merge_method, str) or merge_method not in _MERGE_METHODS:
        raise ValueError("Expected merge_method to be merge, squash, or rebase")
    if not isinstance(expected_head, str) or not _COMMIT_SHA.fullmatch(expected_head):
        raise ValueError("Expected expected_head to be the pull request's head commit SHA")
    facet, reference = _merge_target(session_id, pr_url)
    return facet.merge(root, reference, merge_method=merge_method, expected_head=expected_head)


def disable_pr_auto_merge(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Turn off the selected PR's auto-merge.

    :returns: A ``session.github.pr_merge``.
    :raises ValueError: Without a selected tracked PR, when its provider cannot
        merge, or when the provider refuses.
    """
    facet, reference = _merge_target(session_id, pr_url)
    return facet.disable_auto_merge(root, reference)


def _reviewer_refs(value: object, field: str) -> list[ReviewerRef]:
    """Return a request field's ``{login, kind}`` reviewers, without case-insensitive repeats.

    GitHub logins and team slugs ignore case, so the first spelling sent is kept.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Expected {field} to be a list of reviewers")
    unique: dict[tuple[str, str], ReviewerRef] = {}
    for item in value:
        login = item.get("login") if isinstance(item, dict) else None
        kind = item.get("kind") if isinstance(item, dict) else None
        if not isinstance(login, str) or not login.strip() or kind not in ("user", "team"):
            raise ValueError(f"Expected {field} to be a list of reviewers")
        unique.setdefault((kind, login.casefold()), ReviewerRef(login=login.strip(), kind=kind))
    return list(unique.values())


def pr_review_candidates(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """List the users and teams a review of the selected PR can be requested from.

    :raises ValueError: Without a selected tracked PR, or when its provider
        cannot edit reviewers.
    """
    facet, reference = _reviewer_target(session_id, pr_url)
    return facet.review_candidates(root, reference)


def update_pr_reviewers(
    root: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    add: object = (),
    remove: object = (),
) -> dict[str, Any]:
    """Request reviews on the selected PR and withdraw pending requests.

    :param add: ``{login, kind}`` users and teams to request a review from; one
        who already reviewed is asked again.
    :param remove: ``{login, kind}`` reviewers whose pending request to withdraw;
        one without a request is ignored.
    :returns: A ``session.github.pr_reviewers`` with the PR's resulting reviewers.
    :raises ValueError: On an empty, overlapping, or oversized edit, without a
        selected tracked PR, or when the provider refuses the change.
    """
    add_refs = _reviewer_refs(add, "add")
    remove_refs = _reviewer_refs(remove, "remove")
    if not add_refs and not remove_refs:
        raise ValueError("Expected reviewers to add or remove")
    if {(ref.kind, ref.login.casefold()) for ref in add_refs} & {
        (ref.kind, ref.login.casefold()) for ref in remove_refs
    }:
        raise ValueError("A reviewer cannot be both added and removed")
    if len(add_refs) + len(remove_refs) > _MAX_REVIEWER_EDIT:
        raise ValueError(f"Edit at most {_MAX_REVIEWER_EDIT} reviewers at once")
    facet, reference = _reviewer_target(session_id, pr_url)
    return facet.edit_reviewers(root, reference, add=add_refs, remove=remove_refs)


def create_pr_comment(
    root: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    body: object = None,
) -> dict[str, Any]:
    """Post a non-empty Markdown comment to the selected tracked PR."""
    if not isinstance(body, str):
        raise ValueError("Expected body to be a string")
    if not body.strip():
        raise ValueError("A comment cannot be empty")
    if len(body) > _MAX_BODY_CHARS:
        raise ValueError(f"A comment can be at most {_MAX_BODY_CHARS} characters")
    facet, reference = _capable_target(
        session_id,
        pr_url,
        capable=lambda capabilities: capabilities.comment_posting,
        unselected="Select a pull request to post a comment",
        unsupported="Posting comments on {kind} is not supported",
    )
    return facet.create_comment(root, reference, body=body)


def _details_target(
    session_id: str | None, pr_url: str | None
) -> tuple[PullRequestFacet, SessionPullRequest]:
    """Return the selected tracked PR and its facet, which must support details editing."""
    return _capable_target(
        session_id,
        pr_url,
        capable=lambda capabilities: capabilities.details_editing,
        unselected="Select a pull request to edit its title or description",
        unsupported="Editing {kind} titles and descriptions is not supported",
    )


def update_pr_details(
    root: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    title: object = None,
    body: object = None,
) -> dict[str, Any]:
    """Replace the selected PR's title and/or description.

    :param title: The new title, or ``None`` to keep it; surrounding space is dropped.
    :param body: The new description in markdown, or ``None`` to keep it.
    :returns: A ``session.github.pr_details`` with the PR's resulting title and body.
    :raises ValueError: On a blank or oversized title, an oversized body, neither
        field set, without a selected tracked PR, or when the provider refuses.
    """
    if title is not None and not isinstance(title, str):
        raise ValueError("Expected title to be a string")
    if body is not None and not isinstance(body, str):
        raise ValueError("Expected body to be a string")
    if title is None and body is None:
        raise ValueError("Expected a title or body to update")
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("A pull request title cannot be empty")
        if len(title) > _MAX_TITLE_CHARS:
            raise ValueError(f"A pull request title can be at most {_MAX_TITLE_CHARS} characters")
    if body is not None and len(body) > _MAX_BODY_CHARS:
        raise ValueError(f"A pull request description can be at most {_MAX_BODY_CHARS} characters")
    facet, reference = _details_target(session_id, pr_url)
    return facet.edit_details(root, reference, title=title, body=body)


def _attachment_media_type(content: bytes) -> tuple[str, str] | None:
    """Return the ``(content_type, extension)`` the bytes start with, or ``None``."""
    for offset, signature, content_type, extension in _ATTACHMENT_SIGNATURES:
        if content[offset : offset + len(signature)] == signature:
            return content_type, extension
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", "webp"
    if content[4:8] == b"ftyp":
        if content[8:12] == _QUICKTIME_BRAND:
            return "video/quicktime", "mov"
        if content[8:12] in _MP4_BRANDS:
            return "video/mp4", "mp4"
    return None


def _attachment_name(name: object, extension: str) -> str:
    """A file name for the upload, ending in ``extension`` as GitHub requires."""
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if isinstance(name, str) else ""
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = re.sub(r"[^\w.-]+", "-", stem).strip("-.")[:100] or "attachment"
    return f"{stem}.{extension}"


def upload_pr_attachment(
    root: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    name: object = None,
    content: bytes = b"",
) -> dict[str, Any]:
    """Upload an image or video to embed in the selected PR's description.

    The media type comes from the content, not the client, since GitHub stores
    whatever type it is told.

    :param name: The file's name; its extension is replaced to match the content.
    :returns: A ``session.github.pr_attachment`` with the ``url`` to embed.
    :raises ValueError: On an empty, unsupported, or oversized file, without a
        selected tracked PR, or when the provider refuses the upload.
    """
    if not content:
        raise ValueError("The attachment is empty")
    media = _attachment_media_type(content)
    if media is None:
        raise ValueError("Attach a PNG, JPEG, GIF, or WebP image, or an MP4, MOV, or WebM video")
    content_type, extension = media
    limit = (
        _MAX_VIDEO_ATTACHMENT_BYTES
        if content_type.startswith("video/")
        else _MAX_IMAGE_ATTACHMENT_BYTES
    )
    if len(content) > limit:
        kind = "A video" if content_type.startswith("video/") else "An image"
        raise ValueError(f"{kind} can be at most {limit // (1024 * 1024)} MB")
    facet, reference = _capable_target(
        session_id,
        pr_url,
        capable=lambda capabilities: capabilities.attachment_uploads,
        unselected="Select a pull request to attach files to",
        unsupported="Attaching files to a {kind} is not supported",
    )
    return facet.upload_attachment(
        root,
        reference,
        name=_attachment_name(name, extension),
        content_type=content_type,
        content=content,
    )


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
