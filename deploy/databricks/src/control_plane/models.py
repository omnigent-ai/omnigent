"""Control-plane ORM models — additive tables on the shared database.

These three tables are created on boot via
:func:`create_control_plane_tables` using ``metadata.create_all`` on a
**separate** declarative base, so the control plane adds its own tables
without touching upstream's Alembic migration chain (the hard
constraint: consume upstream, don't fork its schema). ``create_all`` is
idempotent — existing tables are left alone.

Design mirrors upstream shapes:

- ``agent_acl`` reuses the ``(user_id, resource_id, level)`` triple from
  ``session_permissions`` — here the resource is an agent and the
  "user" may be a group principal (``group:<name>``). Levels reuse the
  same integers (1=read/launch, 4=owner) so the model is familiar.
- ``agent_visibility`` is the per-agent owner + visibility-mode record,
  the agent analogue of session ownership.
- ``agent_audit`` is an append-only log of governed actions.

Column types match upstream conventions (``String(64)`` agent ids,
``String(128)`` user ids, ``Integer`` epoch timestamps) so no new data
type is introduced — see ``IMPLEMENTATION_REPORT.md`` DB-delta section.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Visibility modes.
VISIBILITY_ORG = "org"
VISIBILITY_RESTRICTED = "restricted"

# ACL levels — reuse upstream's numbering so the shape is identical.
# For agents only two are meaningful: LAUNCH (may list + launch) and
# OWNER (the publisher; may manage). Kept numeric and >= comparable.
ACL_LEVEL_LAUNCH = 1
ACL_LEVEL_OWNER = 4

# Group principals in agent_acl are stored as "group:<name>"; user
# principals are bare emails. This prefix is the only sentinel.
GROUP_PRINCIPAL_PREFIX = "group:"


class ControlPlaneBase(DeclarativeBase):
    """Declarative base for control-plane tables only.

    Separate from upstream's ``omnigent.db.db_models.Base`` so
    ``create_all`` here touches *only* control-plane tables and never
    races or conflicts with upstream's Alembic-managed schema.
    """


class SqlAgentVisibility(ControlPlaneBase):
    """Per-agent owner + visibility mode.

    One row per *template* (built-in) agent the control plane governs.
    Absence of a row means the agent is treated as org-visible and
    unowned (back-compat for operator-seeded agents that predate the
    control plane).

    :param agent_id: The template agent id, e.g. ``"ag_abc123"``. PK.
    :param owner_id: Email of the owner/publisher set by the platform at
        publish time, e.g. ``"alice@databricks.com"``. ``None`` for
        operator-seeded agents.
    :param visibility: ``"org"`` (all org users) or ``"restricted"``
        (only the audience in ``agent_acl``).
    :param created_at: Unix epoch seconds when the row was created.
    :param updated_at: Unix epoch seconds of the last visibility change.
    """

    __tablename__ = "cp_agent_visibility"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'org'")
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "visibility IN ('org', 'restricted')",
            name="ck_cp_agent_visibility_mode",
        ),
    )


class SqlAgentAcl(ControlPlaneBase):
    """Agent access-control entry — the ``(principal, agent, level)`` triple.

    Mirrors ``session_permissions``'s ``(user_id, conversation_id,
    level)`` shape exactly, with the resource being an agent and the
    principal being either a user email or a ``group:<name>`` token. Only
    consulted when an agent's visibility is ``"restricted"``.

    PK is ``(principal, agent_id)`` — optimized for "does this principal
    see this agent" point lookups and "who is the audience for this
    agent" prefix scans on ``agent_id``.

    :param principal: A user email (``"bob@x.com"``) or a group token
        (``"group:fsi-team"``).
    :param agent_id: The template agent id, e.g. ``"ag_abc123"``.
    :param level: Numeric level: ``1`` = launch (may list + launch),
        ``4`` = owner. ``>=`` comparable, same as upstream.
    """

    __tablename__ = "cp_agent_acl"

    principal: Mapped[str] = mapped_column(String(160), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("level IN (1, 4)", name="ck_cp_agent_acl_level"),
        Index("ix_cp_agent_acl_agent_id", "agent_id"),
    )


class SqlAgentAudit(ControlPlaneBase):
    """Append-only audit log of governed control-plane actions.

    :param id: Autoincrement primary key.
    :param ts: Unix epoch seconds of the action.
    :param actor: Email of the user who performed the action.
    :param action: Short action name, e.g. ``"publish"`` or
        ``"visibility_change"``.
    :param agent_id: The affected agent id, or ``None`` for non-agent
        actions.
    :param detail: Free-text detail, e.g. ``"visibility=restricted
        users=1 groups=1"``.
    """

    __tablename__ = "cp_agent_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_cp_agent_audit_ts", "ts"),)


def create_control_plane_tables(engine: Engine) -> None:
    """Create control-plane tables if they don't exist.

    Idempotent: ``create_all`` issues ``CREATE TABLE IF NOT EXISTS`` per
    table and skips ones that already exist. Safe to call on every boot.
    Uses the same engine as the upstream stores (shared Lakebase pool +
    token hook), so no extra connection configuration is needed.

    :param engine: The shared SQLAlchemy engine from
        ``get_or_create_engine``.
    """
    ControlPlaneBase.metadata.create_all(engine)
