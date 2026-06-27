"""Unit tests for role resolution (feature 1: three-tier model).

Covers the group→role mapping policy, the native-admin precedence, the
consumer default, and the capability flags — including the critical
"consumer is use-only" assertion.
"""

from __future__ import annotations

import control_plane.identity as cp_identity
from control_plane.config import ControlPlaneConfig, capabilities_for_role
from control_plane.roles import RoleResolver


def _config() -> ControlPlaneConfig:
    return ControlPlaneConfig(
        admin_groups=frozenset({"platform-admins"}),
        contributor_groups=frozenset({"gtm-contributors"}),
        admin_users=frozenset({"founder@db.com"}),
        groups_enabled=True,
    )


def _resolver(native_admins: set[str] | None = None) -> RoleResolver:
    native = native_admins or set()
    return RoleResolver(
        _config(),
        auth_provider=None,
        native_admin_lookup=lambda uid: uid in native,
    )


def test_consumer_is_default_and_use_only() -> None:
    """A user in no special group resolves to consumer with no write caps."""
    cp_identity.set_group_overrides({"dave@db.com": ["random-team"]})
    try:
        p = _resolver().resolve_for_user("dave@db.com")
    finally:
        cp_identity.set_group_overrides({})
    assert p.role == "consumer"
    assert p.can_publish is False
    assert p.capabilities["can_manage_visibility"] is False
    assert p.capabilities["can_view_usage"] is False


def test_contributor_group_confers_contributor() -> None:
    cp_identity.set_group_overrides({"carol@db.com": ["gtm-contributors"]})
    try:
        p = _resolver().resolve_for_user("carol@db.com")
    finally:
        cp_identity.set_group_overrides({})
    assert p.role == "contributor"
    assert p.can_publish is True
    assert p.capabilities["can_manage_all"] is False


def test_admin_group_confers_admin() -> None:
    cp_identity.set_group_overrides({"sam@db.com": ["platform-admins"]})
    try:
        p = _resolver().resolve_for_user("sam@db.com")
    finally:
        cp_identity.set_group_overrides({})
    assert p.role == "admin"
    assert p.capabilities["can_manage_all"] is True


def test_admin_user_list_confers_admin_without_group() -> None:
    cp_identity.set_group_overrides({})
    p = _resolver().resolve_for_user("founder@db.com")
    assert p.role == "admin"


def test_native_admin_flag_confers_admin() -> None:
    """Native is_admin (platform flag) always maps to admin role."""
    cp_identity.set_group_overrides({})
    p = _resolver(native_admins={"ops@db.com"}).resolve_for_user("ops@db.com")
    assert p.role == "admin"
    assert p.is_platform_admin is True


def test_admin_precedence_over_contributor() -> None:
    """A user in both groups resolves to the higher role (admin)."""
    cp_identity.set_group_overrides({"x@db.com": ["gtm-contributors", "platform-admins"]})
    try:
        p = _resolver().resolve_for_user("x@db.com")
    finally:
        cp_identity.set_group_overrides({})
    assert p.role == "admin"


def test_unauthenticated_resolves_to_consumer_none() -> None:
    p = _resolver().resolve_for_user(None)
    assert p.user_id is None
    assert p.role == "consumer"


def test_groups_disabled_skips_resolution() -> None:
    """With groups disabled, only admin-user/native confer admin; groups ignored."""
    cfg = ControlPlaneConfig(
        contributor_groups=frozenset({"gtm-contributors"}),
        groups_enabled=False,
    )
    cp_identity.set_group_overrides({"carol@db.com": ["gtm-contributors"]})
    try:
        r = RoleResolver(cfg, auth_provider=None, native_admin_lookup=None)
        p = r.resolve_for_user("carol@db.com")
    finally:
        cp_identity.set_group_overrides({})
    # groups not consulted → consumer despite the override
    assert p.role == "consumer"


def test_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_CP_ADMIN_GROUPS", "Plat-Admins, ops")
    monkeypatch.setenv("OMNIGENT_CP_CONTRIBUTOR_GROUPS", "builders")
    monkeypatch.setenv("OMNIGENT_CP_ADMIN_USERS", "Boss@DB.com")
    cfg = ControlPlaneConfig.from_env()
    assert cfg.admin_groups == frozenset({"plat-admins", "ops"})
    assert cfg.contributor_groups == frozenset({"builders"})
    assert cfg.admin_users == frozenset({"boss@db.com"})
    assert cfg.groups_enabled is True


def test_capabilities_table() -> None:
    assert capabilities_for_role("admin")["can_manage_all"] is True
    assert capabilities_for_role("contributor")["can_publish"] is True
    assert capabilities_for_role("contributor")["can_manage_all"] is False
    assert capabilities_for_role("consumer") == {
        "can_publish": False,
        "can_manage_visibility": False,
        "can_view_usage": False,
        "can_manage_all": False,
    }
