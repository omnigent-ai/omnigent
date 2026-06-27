"""Role resolution — turn a request into a resolved principal.

Ties together the three inputs to the three-tier model:

1. identity (email) from the upstream auth provider (``X-Forwarded-Email``);
2. group membership from :mod:`control_plane.identity` (SCIM, cached);
3. the native ``is_admin`` flag from the upstream permission store.

…and applies the deployment's :class:`~control_plane.config.ControlPlaneConfig`
policy to produce a :class:`ResolvedPrincipal` that the API routes and the
enforcement middleware both consume. This is the *single* place role logic
lives, so the API, the UI's ``/me`` response, and enforcement can never
disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import HTTPConnection

from control_plane.config import ControlPlaneConfig, capabilities_for_role
from control_plane.identity import resolve_groups
from omnigent.server.auth import AuthProvider


@dataclass(frozen=True)
class ResolvedPrincipal:
    """A caller resolved to identity, groups, role, and capabilities.

    :param user_id: The caller's email, or ``None`` if unauthenticated.
    :param role: ``"admin"`` / ``"contributor"`` / ``"consumer"``.
        ``consumer`` for unauthenticated callers (they can't do anything
        anyway — routes enforce auth separately).
    :param groups: The caller's normalized group names.
    :param is_platform_admin: The native upstream ``is_admin`` flag.
    :param capabilities: Capability flags derived from ``role``.
    """

    user_id: str | None
    role: str
    groups: frozenset[str]
    is_platform_admin: bool
    capabilities: dict[str, bool]

    @property
    def is_admin(self) -> bool:
        """Whether this principal has the admin role (group or native)."""
        return self.role == "admin"

    @property
    def can_publish(self) -> bool:
        return bool(self.capabilities.get("can_publish"))

    def to_me_dict(self) -> dict:
        """Serialize for ``GET /v1/control-plane/me``."""
        return {
            "user_id": self.user_id,
            "role": self.role,
            "groups": sorted(self.groups),
            "is_platform_admin": self.is_platform_admin,
            "capabilities": self.capabilities,
        }


class RoleResolver:
    """Resolves requests to :class:`ResolvedPrincipal` using one policy.

    :param config: The deployment's role/group policy.
    :param auth_provider: Upstream auth provider for identity extraction.
    :param native_admin_lookup: Callable ``email -> bool`` returning the
        upstream ``is_admin`` flag (typically
        ``permission_store.is_admin``). May be ``None`` to skip the native
        check (then only group/admin-user config confers admin).
    """

    def __init__(
        self,
        config: ControlPlaneConfig,
        auth_provider: AuthProvider | None,
        native_admin_lookup=None,
    ) -> None:
        self._config = config
        self._auth_provider = auth_provider
        self._native_admin_lookup = native_admin_lookup

    def user_id(self, conn: HTTPConnection) -> str | None:
        """Extract the caller's identity from the request."""
        if self._auth_provider is None:
            return None
        return self._auth_provider.get_user_id(conn)

    def resolve(self, conn: HTTPConnection) -> ResolvedPrincipal:
        """Resolve a request to a principal.

        Never raises — an unauthenticated caller resolves to a
        ``consumer`` with ``user_id=None`` (routes that require auth
        return 401 themselves).

        :param conn: The incoming request/connection.
        :returns: The :class:`ResolvedPrincipal`.
        """
        uid = self.user_id(conn)
        return self.resolve_for_user(uid)

    def resolve_for_user(self, uid: str | None) -> ResolvedPrincipal:
        """Resolve a principal from an already-extracted user id.

        Split out so the enforcement middleware (which has the id in
        hand) and tests can resolve without a request object.

        :param uid: The caller's email, or ``None``.
        :returns: The :class:`ResolvedPrincipal`.
        """
        if uid is None:
            return ResolvedPrincipal(
                user_id=None,
                role="consumer",
                groups=frozenset(),
                is_platform_admin=False,
                capabilities=capabilities_for_role("consumer"),
            )
        native_admin = False
        if self._native_admin_lookup is not None:
            try:
                native_admin = bool(self._native_admin_lookup(uid))
            except Exception:  # noqa: BLE001 — a lookup failure must not deny the request
                native_admin = False
        groups = resolve_groups(uid) if self._config.groups_enabled else frozenset()
        role = self._config.role_for(user_id=uid, groups=groups, native_admin=native_admin)
        return ResolvedPrincipal(
            user_id=uid,
            role=role,
            groups=groups,
            is_platform_admin=native_admin,
            capabilities=capabilities_for_role(role),
        )
