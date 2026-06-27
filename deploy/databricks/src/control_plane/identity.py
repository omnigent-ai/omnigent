"""Identity / group resolution for the control plane.

Upstream resolves only the caller's *email* (from ``X-Forwarded-Email``
in Databricks Apps header mode); it never fetches SCIM group membership.
The role model needs groups, so this module fetches them from Databricks
SCIM via the app's ambient ``WorkspaceClient`` (the app runs as a service
principal with directory read access) and caches the result.

Caching matters: a SCIM lookup is a synchronous HTTPS round-trip, and the
role is resolved on a hot path (every control-plane request, plus
enforcement on agent-list and session-create). Group membership changes
rarely, so a short TTL cache (default 5 min) keeps the hot path fast
while staying fresh enough for an admin/contributor change to take effect
within minutes.

For tests and non-Databricks hosts, a static override map can be
injected (``set_group_overrides``) so no SDK call happens — the resolver
is fully usable without a workspace.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("omnigent-app.control_plane.identity")

# Optional static overrides: email (lowercased) -> list of group names.
# When set for a user, these are returned verbatim and no SDK call is
# made. Used by tests and by deployments that want to pin membership.
_overrides: dict[str, frozenset[str]] = {}
_overrides_lock = threading.Lock()

# TTL cache: email -> (groups, expiry_monotonic).
_CACHE_TTL_SECONDS = 5 * 60
# Negative-result TTL: a *transient* SCIM fetch failure (network blip, 429/5xx,
# token expiry, lost directory access) is indistinguishable from a genuine
# empty group set, but must not be cached as a membership fact for the full
# TTL — that would silently demote a group-derived admin/contributor to
# ``consumer`` for 5 minutes on one blip. Cache the degraded result only
# briefly so a privileged user self-heals within seconds, while still
# shielding SCIM from being hammered every request during a sustained outage.
_NEGATIVE_CACHE_TTL_SECONDS = 15
_cache: dict[str, tuple[frozenset[str], float]] = {}
_cache_lock = threading.Lock()


class GroupFetchError(Exception):
    """Raised by a group fetcher when membership could not be determined.

    Signals a *transient* failure (distinct from a user who genuinely has
    no groups, which is an empty frozenset), so :func:`resolve_groups` can
    degrade for the current request without poisoning the cache for the
    full TTL.
    """

# Lazily-built workspace-client group fetcher; injectable for tests.
_group_fetcher: Callable[[str], frozenset[str]] | None = None
_group_fetcher_lock = threading.Lock()


def set_group_overrides(overrides: dict[str, list[str]]) -> None:
    """Install static email→groups overrides (replaces any existing).

    :param overrides: Mapping of email to the user's group names. Keys
        and group names are normalized (lowercased/stripped) on store.
    """
    normalized = {
        email.strip().lower(): frozenset(g.strip().lower() for g in groups if g.strip())
        for email, groups in overrides.items()
    }
    with _overrides_lock:
        _overrides.clear()
        _overrides.update(normalized)
    # Overrides supersede cached SDK results — drop the cache.
    with _cache_lock:
        _cache.clear()


def set_group_fetcher(fetcher: Callable[[str], frozenset[str]] | None) -> None:
    """Inject the function that fetches a user's groups by email.

    Primarily for tests (inject a fake) but also lets the deploy entry
    point pass a pre-built Databricks fetcher. When ``None``, the
    resolver builds the default :func:`_databricks_group_fetcher` lazily
    on first need.

    :param fetcher: ``email -> frozenset[group_name]`` or ``None``.
    """
    global _group_fetcher
    with _group_fetcher_lock:
        _group_fetcher = fetcher
    with _cache_lock:
        _cache.clear()


def clear_cache() -> None:
    """Drop all cached group memberships (test hygiene)."""
    with _cache_lock:
        _cache.clear()


def _databricks_group_fetcher(email: str) -> frozenset[str]:
    """Resolve a user's group names from Databricks SCIM.

    Looks the user up by email (``userName``) and returns the display
    names of their direct group memberships. A genuine "no groups" result
    (SDK absent, or the user has no memberships) yields an empty set. A
    *transient* failure (no directory access, network/SDK error) raises
    :class:`GroupFetchError` so the caller degrades for this request
    without caching the empty set as a durable membership fact.

    :param email: The caller's email, lowercased.
    :returns: Normalized (lowercased) group display names.
    :raises GroupFetchError: on a transient lookup failure.
    """
    try:
        from databricks.sdk import WorkspaceClient
    except ModuleNotFoundError:
        # No Databricks SDK on this host (local/test/OSS). Expected and
        # stable — group resolution is a genuine no-op, safe to cache.
        logger.debug("control_plane.identity: databricks SDK absent; no groups for %s", email)
        return frozenset()

    try:
        wc = WorkspaceClient()
        # SCIM list with a userName filter. The SDK paginates; we read
        # the first match.
        users = list(
            wc.users.list(
                filter=f'userName eq "{email}"',
                attributes="groups,userName",
                count=1,
            )
        )
    except Exception as exc:  # noqa: BLE001 — transient: degrade, don't poison cache
        logger.warning(
            "control_plane.identity: SCIM group fetch failed for %s; "
            "degrading to no groups for this request only",
            email,
            exc_info=True,
        )
        raise GroupFetchError(email) from exc

    if not users:
        # The directory was reachable and returned no match — a genuine,
        # stable "no such user / no groups" result, safe to cache.
        logger.info("control_plane.identity: no SCIM user for %s", email)
        return frozenset()
    groups = users[0].groups or []
    resolved = frozenset(
        (g.display or "").strip().lower() for g in groups if (g.display or "").strip()
    )
    if not resolved:
        # The user matched but carries NO group attribute. This is a genuine
        # empty for a truly group-less user — but on Databricks the `groups`
        # sub-attribute is only returned to a workspace-ADMIN caller, so an
        # empty here often means the app service principal lacks workspace-
        # admin SCIM read. Surface that explicitly: group-derived roles will
        # silently fall back to consumer until the SP is granted admin. We
        # still return frozenset() (a stable result — do NOT raise, so it
        # isn't thrashed through the transient negative cache).
        logger.info(
            "control_plane.identity: user %s matched but has no SCIM groups. If this user "
            "IS in a role group, the app service principal likely lacks workspace-admin "
            "SCIM group-read — add the SP to the workspace 'admins' group so group-derived "
            "roles resolve.",
            email,
        )
    return resolved


def _get_fetcher() -> Callable[[str], frozenset[str]]:
    """Return the active group fetcher, building the default lazily."""
    global _group_fetcher
    with _group_fetcher_lock:
        if _group_fetcher is None:
            _group_fetcher = _databricks_group_fetcher
        return _group_fetcher


def resolve_groups(email: str) -> frozenset[str]:
    """Return a user's normalized group names, cached with a short TTL.

    Resolution order: static override (no SDK call) → TTL cache → group
    fetcher (SDK). Never raises — a fetch failure yields an empty set.

    :param email: The caller's email (any case); normalized internally.
    :returns: Frozenset of lowercased group names (possibly empty).
    """
    key = email.strip().lower()
    if not key:
        return frozenset()

    with _overrides_lock:
        if key in _overrides:
            return _overrides[key]

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

    try:
        groups = _get_fetcher()(key)
        ttl = _CACHE_TTL_SECONDS
    except GroupFetchError:
        # Transient failure: degrade to no groups for THIS request, but only
        # cache it briefly so a privileged user isn't pinned to ``consumer``
        # for the full TTL on one blip. A custom fetcher that raises a
        # GroupFetchError opts into the same negative-TTL behavior.
        groups = frozenset()
        ttl = _NEGATIVE_CACHE_TTL_SECONDS

    with _cache_lock:
        _cache[key] = (groups, now + ttl)
    return groups
