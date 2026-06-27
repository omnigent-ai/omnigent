"""Per-agent usage / cost read-model.

Feature 5: org-wide visibility into who runs which agents and at what
cost, attributed *to the agent*. Cost is already recorded on the
existing path — ``conversations.session_usage`` (a JSON blob written at
each turn boundary by the upstream cost-write sites) — and session
ownership lives in ``session_permissions``. There is no per-agent
rollup table upstream, so this module computes the aggregation as a
**read-model** over those existing tables: no new write path, no change
to the hot cost path (honoring "reuse the existing cost path").

The read groups top-level sessions by ``agent_id``, sums each session's
``session_usage.total_cost_usd`` / ``total_tokens``, and attributes the
spend to the session owner (the ``LEVEL_OWNER`` grantee). Agent names
come from the ``agents`` table.

Aggregation is computed in Python after a couple of bounded queries
rather than in SQL because ``session_usage`` is a JSON string column
(stored as ``Text`` for SQLite compatibility) — parsing it in the app
keeps the query portable across SQLite (tests) and PostgreSQL
(Lakebase).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from control_plane.acl_store import AgentAclStore
from omnigent.db.db_models import SqlAgent, SqlConversation, SqlSessionPermission
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.server.auth import RESERVED_USER_PUBLIC

logger = logging.getLogger("omnigent-app.control_plane.usage")


@dataclass
class _Accum:
    """Mutable accumulator for one agent's usage during aggregation."""

    cost_usd: float = 0.0
    total_tokens: int = 0
    session_count: int = 0
    by_user: dict[str, dict] = field(
        default_factory=lambda: defaultdict(
            lambda: {"cost_usd": 0.0, "total_tokens": 0, "session_count": 0}
        )
    )


def _parse_usage(blob: str | None) -> tuple[float, int]:
    """Extract ``(total_cost_usd, total_tokens)`` from a session_usage blob.

    Tolerant of missing / malformed JSON (returns zeros) so one bad row
    never breaks the report.

    :param blob: The raw ``session_usage`` text, or ``None``.
    :returns: ``(cost_usd, total_tokens)``.
    """
    if not blob:
        return 0.0, 0
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return 0.0, 0
    cost = data.get("total_cost_usd") or 0.0
    tokens = data.get("total_tokens") or 0
    try:
        return float(cost), int(tokens)
    except (ValueError, TypeError):
        return 0.0, 0


class UsageReporter:
    """Computes per-agent usage / cost aggregations from existing tables.

    :param storage_location: SQLAlchemy DB URI; shares the engine/pool.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def report(
        self,
        *,
        agent_id: str | None = None,
        acl_store=None,
        user_id: str = "",
        groups=frozenset(),
        is_admin: bool = False,
    ) -> dict:
        """Build the per-agent usage report, filtered to what the caller may see.

        :param agent_id: If set, restrict to a single agent (drill-down);
            otherwise report every agent that has at least one session.
        :param acl_store: Agent ACL store used to filter rows by visibility.
            When ``None`` (e.g. direct unit-test calls), no filtering/redaction
            is applied — the legacy admin-everything behavior.
        :param user_id: The caller's id (for ``can_view`` / owner checks).
        :param groups: The caller's groups (for ``can_view``).
        :param is_admin: Whether the caller is an admin (sees + un-redacted all).
        :returns: ``{"data": [...per-agent...], "totals": {...}}`` matching
            the API contract.

        Filtering: a non-admin caller only sees rows for agents they
        ``can_view`` (org agents + restricted agents they own / are in the
        audience for), matching the ``GET /v1/agents`` middleware filter. The
        per-user ``by_user`` breakdown is redacted unless the caller is admin
        or the agent's owner, so a contributor never sees other users' spend.
        Totals are computed over the surfaced rows only, so hidden agents'
        spend doesn't leak via the aggregate.
        """
        with self._session() as session:
            # Top-level sessions only (no sub-agent double-counting); must
            # have an agent bound and some usage recorded. Join SqlAgent to
            # also read bundle_location, which carries the template lineage
            # for fork/switch clones (see _template_id_for).
            conv_stmt = (
                select(
                    SqlConversation.id,
                    SqlConversation.agent_id,
                    SqlConversation.session_usage,
                    SqlAgent.bundle_location,
                )
                .join(SqlAgent, SqlConversation.agent_id == SqlAgent.id, isouter=True)
                .where(
                    SqlConversation.agent_id.is_not(None),
                    SqlConversation.parent_conversation_id.is_(None),
                )
            )
            conv_rows = session.execute(conv_stmt).all()

            conv_ids = [r.id for r in conv_rows]
            owners = self._owners_for(session, conv_ids)
            agent_names = self._agent_names(session)
            template_ids = self._template_ids(session)

        accums: dict[str, _Accum] = defaultdict(_Accum)
        for row in conv_rows:
            # Roll fork/switch clones up to the template they were cloned from
            # (bundle_location prefix), so per-template cost/usage stays
            # attributed to the governed catalog agent, not the clone id.
            aid = self._template_id_for(row.bundle_location, row.agent_id, template_ids)
            # Drill-down filter is applied AFTER lineage resolution so a query
            # for a template id captures its forks/switches too.
            if agent_id is not None and aid != agent_id:
                continue
            cost, tokens = _parse_usage(row.session_usage)
            acc = accums[aid]
            acc.session_count += 1
            acc.cost_usd += cost
            acc.total_tokens += tokens
            owner = owners.get(row.id) or "(unattributed)"
            u = acc.by_user[owner]
            u["cost_usd"] += cost
            u["total_tokens"] += tokens
            u["session_count"] += 1

        # Resolve visibility for the grouped (template-lineage) ids in one
        # batch, so we can filter rows the caller may not see and decide
        # per-row whether to expose by_user. Skipped when no acl_store (legacy
        # admin-everything path) or when the caller is admin (sees all).
        vis_map = {}
        if acl_store is not None and not is_admin:
            vis_map = acl_store.get_visibility_map(list(accums.keys()))

        data = []
        tot_cost = 0.0
        tot_tokens = 0
        tot_sessions = 0
        for aid, acc in sorted(accums.items(), key=lambda kv: kv[1].cost_usd, reverse=True):
            # Visibility filter (non-admin only): drop agents the caller can't
            # view — same predicate as the GET /v1/agents middleware filter.
            expose_by_user = True
            if acl_store is not None and not is_admin:
                vis = vis_map.get(aid)
                if vis is not None:
                    if not AgentAclStore.can_view(
                        vis, user_id=user_id, groups=groups, is_admin=is_admin
                    ):
                        continue
                    # by_user (other users' emails/spend) only for the owner.
                    expose_by_user = AgentAclStore.can_manage(
                        vis, user_id=user_id, is_admin=is_admin
                    )
            by_user = (
                [
                    {
                        "user_id": uid,
                        "cost_usd": round(v["cost_usd"], 6),
                        "total_tokens": v["total_tokens"],
                        "session_count": v["session_count"],
                    }
                    for uid, v in sorted(
                        acc.by_user.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
                    )
                ]
                if expose_by_user
                else []
            )
            data.append(
                {
                    "agent_id": aid,
                    "agent_name": agent_names.get(aid, aid),
                    "total_cost_usd": round(acc.cost_usd, 6),
                    "total_tokens": acc.total_tokens,
                    "session_count": acc.session_count,
                    "by_user": by_user,
                }
            )
            # Totals over surfaced rows only — don't leak hidden agents' spend.
            tot_cost += acc.cost_usd
            tot_tokens += acc.total_tokens
            tot_sessions += acc.session_count

        return {
            "data": data,
            "totals": {
                "total_cost_usd": round(tot_cost, 6),
                "total_tokens": tot_tokens,
                "session_count": tot_sessions,
            },
        }

    def _owners_for(self, session, conv_ids: list[str]) -> dict[str, str]:
        """Map each conversation id to its owner (highest-level grantee)."""
        if not conv_ids:
            return {}
        rows = session.execute(
            select(
                SqlSessionPermission.conversation_id,
                SqlSessionPermission.user_id,
                SqlSessionPermission.level,
            )
            .where(SqlSessionPermission.conversation_id.in_(conv_ids))
            .where(SqlSessionPermission.user_id != RESERVED_USER_PUBLIC)
            .order_by(SqlSessionPermission.level.desc())
        ).all()
        owners: dict[str, str] = {}
        for cid, uid, _level in rows:
            # First seen per conversation is the highest level (ordered desc).
            owners.setdefault(cid, uid)
        return owners

    def _agent_names(self, session) -> dict[str, str]:
        """Map agent id → name for every agent (small table)."""
        rows = session.execute(select(SqlAgent.id, SqlAgent.name)).all()
        return dict(rows)

    def _template_ids(self, session) -> frozenset[str]:
        """Ids of template (built-in) agents — ``session_id IS NULL``.

        These are the governed catalog agents; fork/switch clones bake a
        template's id into their ``bundle_location`` prefix, so usage can be
        rolled back up to the template via :meth:`_template_id_for`.
        """
        rows = session.execute(
            select(SqlAgent.id).where(SqlAgent.session_id.is_(None))
        ).all()
        return frozenset(r[0] for r in rows)

    @staticmethod
    def _template_id_for(
        bundle_location: str | None, agent_id: str, template_ids: frozenset[str]
    ) -> str:
        """Resolve a conversation's usage-grouping key to its template lineage.

        Fork/switch clone a built-in into a fresh session-scoped agent whose
        ``bundle_location`` is ``"{templateAgentId}/{sha}"`` — the source
        template id is the prefix. If that prefix is a known template, group
        the clone's usage under the template (collapsing every fork/switch of
        it onto one row). Otherwise fall back to the conversation's own
        ``agent_id`` (native session-scoped agents and published agents whose
        bundle prefix isn't a live template keep per-agent grouping, exactly
        as before).
        """
        if bundle_location:
            prefix = bundle_location.split("/", 1)[0]
            if prefix in template_ids:
                return prefix
        return agent_id
