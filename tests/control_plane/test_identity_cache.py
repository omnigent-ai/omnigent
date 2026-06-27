"""Unit tests for group-resolution caching (identity.py).

Focus on the failure-vs-empty distinction: a *transient* SCIM fetch
failure must degrade to "no groups" for the current request without being
cached as a durable membership fact for the full TTL (which would silently
demote a group-derived admin/contributor to ``consumer`` for minutes).
"""

from __future__ import annotations

import control_plane.identity as cp_identity
from control_plane.identity import GroupFetchError


def _reset() -> None:
    cp_identity.set_group_overrides({})
    cp_identity.set_group_fetcher(None)
    cp_identity.clear_cache()


def test_successful_fetch_is_cached_for_full_ttl() -> None:
    """A genuine result is cached: the fetcher is called once across calls."""
    _reset()
    calls = {"n": 0}

    def fetcher(email: str) -> frozenset[str]:
        calls["n"] += 1
        return frozenset({"gtm-contributors"})

    cp_identity.set_group_fetcher(fetcher)
    try:
        assert cp_identity.resolve_groups("carol@db.com") == frozenset({"gtm-contributors"})
        assert cp_identity.resolve_groups("carol@db.com") == frozenset({"gtm-contributors"})
        assert calls["n"] == 1, "successful result should be cached, not re-fetched"
    finally:
        _reset()


def test_transient_failure_degrades_but_is_not_cached_for_full_ttl() -> None:
    """A GroupFetchError yields empty groups now, but the NEXT call re-fetches
    (so a privileged user recovers quickly) rather than being pinned to the
    cached empty set for the full positive TTL."""
    _reset()
    calls = {"n": 0}

    def flaky(email: str) -> frozenset[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise GroupFetchError(email)  # transient blip on first call
        return frozenset({"platform-admins"})  # recovered

    cp_identity.set_group_fetcher(flaky)
    try:
        # First call: transient failure → degrades to empty.
        assert cp_identity.resolve_groups("sam@db.com") == frozenset()
        # The negative result is cached only briefly. Simulate the short TTL
        # expiring by clearing the cache (equivalent to >15s elapsed), then
        # the next call recovers the real membership.
        cp_identity.clear_cache()
        assert cp_identity.resolve_groups("sam@db.com") == frozenset({"platform-admins"})
        assert calls["n"] == 2
    finally:
        _reset()


def test_negative_ttl_is_shorter_than_positive_ttl() -> None:
    """The failure-cache TTL must be much shorter than the success TTL so a
    blip can't pin a privileged user to consumer for the full window."""
    assert cp_identity._NEGATIVE_CACHE_TTL_SECONDS < cp_identity._CACHE_TTL_SECONDS
    assert cp_identity._NEGATIVE_CACHE_TTL_SECONDS <= 30


def test_genuine_empty_is_distinct_from_failure() -> None:
    """A fetcher returning an empty set (genuine no-groups) is cached for the
    full TTL — only a raised GroupFetchError is treated as transient."""
    _reset()
    calls = {"n": 0}

    def empty(email: str) -> frozenset[str]:
        calls["n"] += 1
        return frozenset()

    cp_identity.set_group_fetcher(empty)
    try:
        assert cp_identity.resolve_groups("dave@db.com") == frozenset()
        assert cp_identity.resolve_groups("dave@db.com") == frozenset()
        assert calls["n"] == 1, "genuine empty result should be cached like any success"
    finally:
        _reset()


def test_matched_user_with_empty_groups_cached_as_genuine_empty() -> None:
    """Option B hardening: when SCIM returns a user but no group attribute
    (the SP-lacks-admin case), it is treated as a STABLE empty — cached for
    the full TTL, NOT raised as a transient GroupFetchError (which would
    thrash the 15s negative cache on every request)."""
    _reset()
    calls = {"n": 0}

    def empty(email: str) -> frozenset[str]:
        # The real _databricks_group_fetcher returns frozenset() (does not
        # raise) for a matched user whose groups attribute is empty.
        calls["n"] += 1
        return frozenset()

    cp_identity.set_group_fetcher(empty)
    try:
        assert cp_identity.resolve_groups("admin@db.com") == frozenset()
        assert cp_identity.resolve_groups("admin@db.com") == frozenset()
        # Cached for the full TTL → only one fetch (not re-fetched on the 15s
        # negative-cache cadence a GroupFetchError would trigger).
        assert calls["n"] == 1
    finally:
        _reset()


def test_group_member_resolves_admin_when_scim_returns_groups() -> None:
    """End-to-end: once the SP can read groups, a group member resolves to the
    mapped role independent of the admin_users bootstrap."""
    from control_plane.config import ControlPlaneConfig
    from control_plane.roles import RoleResolver

    _reset()
    cp_identity.set_group_fetcher(lambda email: frozenset({"omnigent-admins"}))
    cfg = ControlPlaneConfig(
        admin_groups=frozenset({"omnigent-admins"}),
        contributor_groups=frozenset({"gtm-contributors"}),
        admin_users=frozenset(),  # NOT in the bootstrap list
        groups_enabled=True,
    )
    try:
        p = RoleResolver(cfg, auth_provider=None, native_admin_lookup=None).resolve_for_user(
            "grouponly@db.com"
        )
        assert p.role == "admin"
        assert "omnigent-admins" in p.groups
    finally:
        _reset()
