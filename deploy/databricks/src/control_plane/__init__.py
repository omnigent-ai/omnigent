"""GTM control plane — a self-managed governance layer in front of the
Omnigent server.

This package implements the five "Features We'd Love to See in the
Omnigent" from the GTM strategy doc, *without forking* the upstream
``omnigent`` runtime:

1. Three-tier role model (admin / contributor / consumer) mapped from
   Databricks workspace identity / SCIM groups.
2. Per-agent visibility (org-wide or restricted to named users/groups).
3. Delegated, governed agent registration (contributors publish to the
   shared catalog; consumers cannot).
4. A single enforcement point in front of the server (agent-list
   filtering + ``agent_id`` authorization at session-create).
5. Org-wide per-agent usage / cost visibility.

Design: the deploy entry point (``deploy/databricks/src/app.py``) builds
the upstream FastAPI app via ``create_app()`` and then *wraps* it — adds
one HTTP middleware (enforcement) and mounts one router
(``/v1/control-plane/*``). New persistence lives in additive tables on
the same Lakebase database, created on boot via
:func:`control_plane.models.create_control_plane_tables`. Nothing in
``omnigent/`` is modified, so we consume upstream cleanly.

The control plane reuses upstream shapes deliberately:

- the ``(user_id, resource_id, level)`` permission triple →
  :class:`control_plane.acl_store.AgentAclStore` (mirrors
  ``SqlAlchemyPermissionStore``);
- the SQLAlchemy engine/session helpers (``get_or_create_engine`` /
  ``make_managed_session_maker``) so the Lakebase token hook and
  connection pool are shared;
- ``AgentStore`` for catalog publication;
- ``conversations.session_usage`` for the per-agent cost read-model.
"""

from __future__ import annotations

__all__ = [
    "ROLE_ADMIN",
    "ROLE_CONTRIBUTOR",
    "ROLE_CONSUMER",
]

ROLE_ADMIN = "admin"
ROLE_CONTRIBUTOR = "contributor"
ROLE_CONSUMER = "consumer"
