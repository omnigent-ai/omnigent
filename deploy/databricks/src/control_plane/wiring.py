"""One-call wiring: bolt the control plane onto an upstream app.

The deploy entry point builds the upstream FastAPI app with
``create_app(...)`` and then calls :func:`attach_control_plane(app, ...)`.
That single call:

1. creates the control-plane tables (idempotent ``create_all``);
2. builds the stores, role resolver, and usage reporter;
3. installs the enforcement middleware (outermost HTTP middleware);
4. mounts the ``/v1/control-plane`` router.

Keeping all of this behind one function means ``app.py`` only grows a few
lines, and the wiring is testable in isolation (the test suite calls the
same function against a ``create_app`` built on a temp SQLite DB).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from control_plane.acl_store import AgentAclStore
from control_plane.audit_store import AuditStore
from control_plane.config import ControlPlaneConfig
from control_plane.enforcement import install_enforcement_middleware
from control_plane.models import create_control_plane_tables
from control_plane.roles import RoleResolver
from control_plane.routes import create_control_plane_router
from control_plane.usage import UsageReporter
from omnigent.db.utils import get_or_create_engine
from omnigent.server.auth import AuthProvider
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger("omnigent-app.control_plane.wiring")


def attach_control_plane(
    app: FastAPI,
    *,
    db_uri: str,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
    config: ControlPlaneConfig | None = None,
    agent_cache=None,
    artifact_store=None,
) -> None:
    """Attach the full control plane to an already-built upstream app.

    :param app: The FastAPI app returned by ``create_app``.
    :param db_uri: SQLAlchemy DB URI (same one the stores use).
    :param agent_store: Upstream agent store.
    :param conversation_store: Upstream conversation store.
    :param permission_store: Upstream permission store (for native
        ``is_admin``); may be ``None`` (then no native-admin bypass).
    :param auth_provider: Upstream auth provider (identity extraction).
    :param config: Role/group policy; defaults to
        :meth:`ControlPlaneConfig.from_env`.
    :param agent_cache: Upstream ``AgentCache`` (loads a bundle to parse its
        spec) — powers the agent connection-test endpoint. Optional; when
        ``None`` the test endpoint reports the loadable check as unavailable.
    :param artifact_store: Upstream ``ArtifactStore`` (HEAD-checks a bundle
        artifact exists) — powers the test endpoint. Optional.
    """
    cfg = config or ControlPlaneConfig.from_env()

    # 1) Tables (idempotent).
    engine = get_or_create_engine(db_uri)
    create_control_plane_tables(engine)

    # 2) Components.
    acl_store = AgentAclStore(db_uri)
    audit_store = AuditStore(db_uri)
    usage_reporter = UsageReporter(db_uri)
    native_admin_lookup = permission_store.is_admin if permission_store is not None else None
    role_resolver = RoleResolver(cfg, auth_provider, native_admin_lookup=native_admin_lookup)

    # 3) Enforcement middleware (outermost — added after create_app).
    install_enforcement_middleware(
        app,
        role_resolver=role_resolver,
        acl_store=acl_store,
        agent_store=agent_store,
        conversation_store=conversation_store,
    )

    # 4) Management + reporting API.
    router = create_control_plane_router(
        role_resolver=role_resolver,
        acl_store=acl_store,
        audit_store=audit_store,
        usage_reporter=usage_reporter,
        agent_store=agent_store,
        conversation_store=conversation_store,
        agent_cache=agent_cache,
        artifact_store=artifact_store,
    )
    app.include_router(router, prefix="/v1/control-plane", tags=["control-plane"])

    # create_app already mounted the SPA static files at "/" — a
    # catch-all that wins over any route registered AFTER it (Starlette
    # matches routes in order). Our router was appended after that mount,
    # so it would 404. Move the root catch-all mount(s) back to the end so
    # the just-added /v1/control-plane routes resolve first. (No-op for an
    # API-only build that has no SPA mount.)
    _demote_root_catch_all(app)

    logger.info(
        "control plane attached: groups_enabled=%s admin_groups=%d "
        "contributor_groups=%d admin_users=%d",
        cfg.groups_enabled,
        len(cfg.admin_groups),
        len(cfg.contributor_groups),
        len(cfg.admin_users),
    )


def _demote_root_catch_all(app: FastAPI) -> None:
    """Reorder routes so a root ``Mount('/')`` stays last.

    The SPA static mount is a catch-all on ``/``; any API route must be
    matched before it. Moves every root mount (normalized path ``""`` or
    ``"/"``) to the tail of the route list, preserving the relative order
    of everything else.
    """
    from starlette.routing import Mount

    routes = app.router.routes
    root_mounts = [
        r for r in routes if isinstance(r, Mount) and getattr(r, "path", None) in ("", "/")
    ]
    if not root_mounts:
        return
    for m in root_mounts:
        routes.remove(m)
    routes.extend(root_mounts)
