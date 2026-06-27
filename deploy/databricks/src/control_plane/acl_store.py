"""Agent visibility + ACL store.

Mirrors :class:`omnigent.stores.permission_store.sqlalchemy_store.SqlAlchemyPermissionStore`
in structure and idiom — same engine/session helpers, same dialect-aware
upsert (SQLite ``ON CONFLICT DO UPDATE`` / PostgreSQL
``ON CONFLICT … DO UPDATE``), same ORM-to-dict conversion inside the
session context to avoid ``DetachedInstanceError``.

Owns two tables:

- ``cp_agent_visibility`` — owner + mode per agent.
- ``cp_agent_acl`` — the ``(principal, agent_id, level)`` audience triple,
  consulted only for ``restricted`` agents.

The core decision helper is :meth:`can_view`: given the agent's
visibility record and the caller's identity + groups, decide whether the
caller may list/launch the agent. This is the single predicate used by
both the enforcement middleware (filter ``GET /v1/agents``, gate
``POST /v1/sessions``) and the management API.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from control_plane.models import (
    ACL_LEVEL_LAUNCH,
    GROUP_PRINCIPAL_PREFIX,
    VISIBILITY_ORG,
    VISIBILITY_RESTRICTED,
    SqlAgentAcl,
    SqlAgentVisibility,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


def group_principal(group_name: str) -> str:
    """Return the ACL principal token for a group name."""
    return f"{GROUP_PRINCIPAL_PREFIX}{group_name.strip().lower()}"


@dataclass(frozen=True)
class AgentVisibility:
    """Resolved visibility record for one agent.

    :param agent_id: The template agent id.
    :param owner_id: Owner email, or ``None`` if unowned.
    :param visibility: ``"org"`` or ``"restricted"``.
    :param audience_users: Audience user emails (restricted only).
    :param audience_groups: Audience group names (restricted only).
    :param created_at: Epoch seconds the record was created.
    :param updated_at: Epoch seconds of the last update, or ``None``.
    """

    agent_id: str
    owner_id: str | None
    visibility: str
    audience_users: tuple[str, ...]
    audience_groups: tuple[str, ...]
    created_at: int
    updated_at: int | None

    @classmethod
    def default_org(cls, agent_id: str) -> AgentVisibility:
        """Return the implicit org-visible, unowned record for an agent
        with no stored visibility row (back-compat for operator-seeded
        agents).
        """
        return cls(
            agent_id=agent_id,
            owner_id=None,
            visibility=VISIBILITY_ORG,
            audience_users=(),
            audience_groups=(),
            created_at=0,
            updated_at=None,
        )


class AgentAclStore:
    """SQLAlchemy-backed store for agent visibility + ACL audience.

    :param storage_location: SQLAlchemy DB URI; shares the engine/pool
        with the upstream stores via :func:`get_or_create_engine`.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    @property
    def _is_sqlite(self) -> bool:
        return self._engine.dialect.name == "sqlite"

    # ── Visibility records ────────────────────────────────────────

    def get_visibility(self, agent_id: str) -> AgentVisibility:
        """Return an agent's visibility record, or the implicit org default.

        Reads the visibility row plus its ACL audience in one session.

        :param agent_id: The template agent id.
        :returns: An :class:`AgentVisibility` (never ``None`` — absent
            rows map to :meth:`AgentVisibility.default_org`).
        """
        with self._session() as session:
            row = session.get(SqlAgentVisibility, agent_id)
            if row is None:
                return AgentVisibility.default_org(agent_id)
            users, groups = self._read_audience(session, agent_id)
            return AgentVisibility(
                agent_id=row.agent_id,
                owner_id=row.owner_id,
                visibility=row.visibility,
                audience_users=users,
                audience_groups=groups,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def get_visibility_map(self, agent_ids: list[str]) -> dict[str, AgentVisibility]:
        """Batch-fetch visibility records for many agents in two queries.

        Agents with no stored row map to the implicit org default. Used
        by the enforcement middleware to filter a whole agent-list page
        without N round-trips.

        :param agent_ids: Template agent ids.
        :returns: ``{agent_id: AgentVisibility}`` covering every input id.
        """
        result: dict[str, AgentVisibility] = {
            aid: AgentVisibility.default_org(aid) for aid in agent_ids
        }
        if not agent_ids:
            return result
        with self._session() as session:
            vis_rows = (
                session.execute(
                    select(SqlAgentVisibility).where(SqlAgentVisibility.agent_id.in_(agent_ids))
                )
                .scalars()
                .all()
            )
            acl_rows = (
                session.execute(select(SqlAgentAcl).where(SqlAgentAcl.agent_id.in_(agent_ids)))
                .scalars()
                .all()
            )
            audience: dict[str, tuple[list[str], list[str]]] = {aid: ([], []) for aid in agent_ids}
            for acl in acl_rows:
                users, groups = audience[acl.agent_id]
                if acl.principal.startswith(GROUP_PRINCIPAL_PREFIX):
                    groups.append(acl.principal[len(GROUP_PRINCIPAL_PREFIX) :])
                else:
                    users.append(acl.principal)
            for row in vis_rows:
                users, groups = audience.get(row.agent_id, ([], []))
                result[row.agent_id] = AgentVisibility(
                    agent_id=row.agent_id,
                    owner_id=row.owner_id,
                    visibility=row.visibility,
                    audience_users=tuple(sorted(users)),
                    audience_groups=tuple(sorted(groups)),
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
        return result

    def _read_audience(self, session, agent_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Read the (users, groups) audience for one agent inside a session."""
        rows = (
            session.execute(select(SqlAgentAcl).where(SqlAgentAcl.agent_id == agent_id))
            .scalars()
            .all()
        )
        users: list[str] = []
        groups: list[str] = []
        for r in rows:
            if r.principal.startswith(GROUP_PRINCIPAL_PREFIX):
                groups.append(r.principal[len(GROUP_PRINCIPAL_PREFIX) :])
            else:
                users.append(r.principal)
        return tuple(sorted(users)), tuple(sorted(groups))

    def set_owner(self, agent_id: str, owner_id: str, *, visibility: str = VISIBILITY_ORG) -> None:
        """Create or update an agent's visibility row with an owner.

        Used at publish time to stamp ownership. Idempotent upsert.

        :param agent_id: The template agent id.
        :param owner_id: Owner email.
        :param visibility: Initial visibility mode.
        """
        now = now_epoch()
        with self._session() as session:
            values = {
                "agent_id": agent_id,
                "owner_id": owner_id,
                "visibility": visibility,
                "created_at": now,
                "updated_at": now,
            }
            insert = sqlite_insert if self._is_sqlite else pg_insert
            stmt = (
                insert(SqlAgentVisibility)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["agent_id"],
                    set_={"owner_id": owner_id, "visibility": visibility, "updated_at": now},
                )
            )
            session.execute(stmt)

    def set_visibility(
        self,
        agent_id: str,
        visibility: str,
        *,
        audience_users: list[str] | None = None,
        audience_groups: list[str] | None = None,
        owner_id: str | None = None,
    ) -> AgentVisibility:
        """Set an agent's visibility mode and (for restricted) its audience.

        Replaces the entire audience for the agent (full set semantics).
        For ``org`` visibility the audience is cleared. Preserves the
        existing owner unless ``owner_id`` is given.

        :param agent_id: The template agent id.
        :param visibility: ``"org"`` or ``"restricted"``.
        :param audience_users: User emails allowed (restricted only).
        :param audience_groups: Group names allowed (restricted only).
        :param owner_id: If set, (re)assign the owner.
        :returns: The resulting :class:`AgentVisibility`.
        :raises ValueError: On an unknown visibility mode.
        """
        if visibility not in (VISIBILITY_ORG, VISIBILITY_RESTRICTED):
            raise ValueError(f"unknown visibility {visibility!r}")
        users = [u.strip().lower() for u in (audience_users or []) if u.strip()]
        groups = [g.strip().lower() for g in (audience_groups or []) if g.strip()]
        now = now_epoch()
        with self._session() as session:
            existing = session.get(SqlAgentVisibility, agent_id)
            effective_owner = (
                owner_id
                if owner_id is not None
                else (existing.owner_id if existing is not None else None)
            )
            created_at = existing.created_at if existing is not None else now
            insert = sqlite_insert if self._is_sqlite else pg_insert
            stmt = (
                insert(SqlAgentVisibility)
                .values(
                    agent_id=agent_id,
                    owner_id=effective_owner,
                    visibility=visibility,
                    created_at=created_at,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["agent_id"],
                    set_={
                        "owner_id": effective_owner,
                        "visibility": visibility,
                        "updated_at": now,
                    },
                )
            )
            session.execute(stmt)
            # Replace the audience wholesale.
            session.execute(delete(SqlAgentAcl).where(SqlAgentAcl.agent_id == agent_id))
            if visibility == VISIBILITY_RESTRICTED:
                for u in users:
                    session.add(
                        SqlAgentAcl(principal=u, agent_id=agent_id, level=ACL_LEVEL_LAUNCH)
                    )
                for g in groups:
                    session.add(
                        SqlAgentAcl(
                            principal=group_principal(g),
                            agent_id=agent_id,
                            level=ACL_LEVEL_LAUNCH,
                        )
                    )
        return self.get_visibility(agent_id)

    def delete_agent(self, agent_id: str) -> None:
        """Remove all control-plane records for an agent (visibility + ACL).

        Called when an agent is deleted so stale rows don't linger.

        :param agent_id: The template agent id.
        """
        with self._session() as session:
            session.execute(delete(SqlAgentAcl).where(SqlAgentAcl.agent_id == agent_id))
            session.execute(
                delete(SqlAgentVisibility).where(SqlAgentVisibility.agent_id == agent_id)
            )

    # ── Decision helper (the single predicate) ────────────────────

    @staticmethod
    def can_view(
        vis: AgentVisibility,
        *,
        user_id: str,
        groups: frozenset[str],
        is_admin: bool,
    ) -> bool:
        """Decide whether a caller may list/launch an agent.

        The single authorization predicate, used by both the enforcement
        middleware and the management API:

        - admins see everything;
        - org-visible agents are seen by everyone;
        - restricted agents are seen by the owner, audience users, and
          members of audience groups.

        :param vis: The agent's resolved visibility record.
        :param user_id: Caller email (any case).
        :param groups: Caller's normalized group names.
        :param is_admin: Whether the caller is a platform admin.
        :returns: ``True`` if the caller may see/launch the agent.
        """
        if is_admin:
            return True
        if vis.visibility == VISIBILITY_ORG:
            return True
        uid = user_id.strip().lower()
        if vis.owner_id is not None and uid == vis.owner_id.strip().lower():
            return True
        if uid in {u.lower() for u in vis.audience_users}:
            return True
        if groups & {g.lower() for g in vis.audience_groups}:
            return True
        return False

    @staticmethod
    def can_manage(
        vis: AgentVisibility,
        *,
        user_id: str,
        is_admin: bool,
    ) -> bool:
        """Whether a caller may change an agent's visibility.

        Admins may manage any agent; otherwise only the owner.

        :param vis: The agent's visibility record.
        :param user_id: Caller email.
        :param is_admin: Whether the caller is a platform admin.
        :returns: ``True`` if the caller may manage the agent.
        """
        if is_admin:
            return True
        return vis.owner_id is not None and user_id.strip().lower() == vis.owner_id.strip().lower()
