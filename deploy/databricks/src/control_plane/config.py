"""Control-plane configuration — role/group mapping from the environment.

The three-tier role model maps Databricks workspace **groups** to roles.
Which groups confer which role is deployment policy, so it is configured
via environment variables (set in ``databricks.yml`` / app config), not
hard-coded:

- ``OMNIGENT_CP_ADMIN_GROUPS`` — comma-separated group names whose
  members are platform admins.
- ``OMNIGENT_CP_CONTRIBUTOR_GROUPS`` — comma-separated group names whose
  members may publish agents and manage visibility of agents they own.
- Everyone else resolves to ``consumer`` (use-only).

Admins may also be named individually via ``OMNIGENT_CP_ADMIN_USERS``
(comma-separated emails) — useful for bootstrapping before SCIM groups
are wired, and mirrors upstream's file-backed admin-list escape hatch.

Native ``is_admin`` (the upstream platform flag) is always treated as
``admin`` regardless of group config — it stays reserved for the
platform team per the hard constraints.

Group resolution itself (email → groups) lives in
:mod:`control_plane.identity`; this module only owns the *policy* of
which groups mean which role.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Capabilities are derived from role, but expressed explicitly so the API
# and UI agree on one source of truth.
_CONSUMER_CAPS = {
    "can_publish": False,
    "can_manage_visibility": False,
    "can_view_usage": False,
    "can_manage_all": False,
}
_CONTRIBUTOR_CAPS = {
    "can_publish": True,
    "can_manage_visibility": True,  # own agents only; enforced per-agent
    "can_view_usage": True,
    "can_manage_all": False,
}
_ADMIN_CAPS = {
    "can_publish": True,
    "can_manage_visibility": True,  # any agent
    "can_view_usage": True,
    "can_manage_all": True,
}

_CAPS_BY_ROLE = {
    "admin": _ADMIN_CAPS,
    "contributor": _CONTRIBUTOR_CAPS,
    "consumer": _CONSUMER_CAPS,
}


def capabilities_for_role(role: str) -> dict[str, bool]:
    """Return the capability flags for a resolved role.

    :param role: One of ``"admin"``, ``"contributor"``, ``"consumer"``.
    :returns: A fresh dict of capability flags (copy, safe to mutate).
    """
    return dict(_CAPS_BY_ROLE.get(role, _CONSUMER_CAPS))


def _split_env(name: str) -> frozenset[str]:
    """Parse a comma-separated env var into a normalized frozenset.

    Values are lowercased and stripped; empties dropped. Missing or
    blank var yields an empty set.

    :param name: Environment variable name.
    :returns: Frozenset of normalized tokens.
    """
    raw = os.environ.get(name, "") or ""
    return frozenset(tok.strip().lower() for tok in raw.split(",") if tok.strip())


@dataclass(frozen=True)
class ControlPlaneConfig:
    """Resolved role/group policy for a deployment.

    :param admin_groups: Group names whose members are admins.
    :param contributor_groups: Group names whose members are contributors.
    :param admin_users: Individual emails always treated as admin.
    :param groups_enabled: Whether SCIM group resolution should be
        attempted at all. When false (no group config and no explicit
        opt-in), the control plane skips the WorkspaceClient SCIM lookup
        entirely and roles come only from ``admin_users`` + native
        ``is_admin`` — keeps the layer inert on non-Databricks hosts.
    """

    admin_groups: frozenset[str] = field(default_factory=frozenset)
    contributor_groups: frozenset[str] = field(default_factory=frozenset)
    admin_users: frozenset[str] = field(default_factory=frozenset)
    groups_enabled: bool = False

    @classmethod
    def from_env(cls) -> ControlPlaneConfig:
        """Build config from ``OMNIGENT_CP_*`` environment variables.

        :returns: A populated :class:`ControlPlaneConfig`.
        """
        admin_groups = _split_env("OMNIGENT_CP_ADMIN_GROUPS")
        contributor_groups = _split_env("OMNIGENT_CP_CONTRIBUTOR_GROUPS")
        admin_users = _split_env("OMNIGENT_CP_ADMIN_USERS")
        # Resolve groups when any group is configured, or when explicitly
        # forced on (e.g. an admin-groups-only deploy still wants SCIM).
        forced = (os.environ.get("OMNIGENT_CP_GROUPS_ENABLED", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        groups_enabled = forced or bool(admin_groups or contributor_groups)
        return cls(
            admin_groups=admin_groups,
            contributor_groups=contributor_groups,
            admin_users=admin_users,
            groups_enabled=groups_enabled,
        )

    def role_for(self, *, user_id: str, groups: frozenset[str], native_admin: bool) -> str:
        """Resolve a user's role from identity, group membership, and the
        native admin flag.

        Precedence (highest wins): native ``is_admin`` or membership in an
        admin group or the explicit admin-user list → ``admin``; else
        membership in a contributor group → ``contributor``; else
        ``consumer``.

        :param user_id: The caller's email/identity (already lowercased
            by the identity resolver).
        :param groups: The caller's normalized (lowercased) group names.
        :param native_admin: The upstream ``users.is_admin`` flag.
        :returns: ``"admin"``, ``"contributor"``, or ``"consumer"``.
        """
        uid = user_id.lower()
        if native_admin or uid in self.admin_users or (groups & self.admin_groups):
            return "admin"
        if groups & self.contributor_groups:
            return "contributor"
        return "consumer"
