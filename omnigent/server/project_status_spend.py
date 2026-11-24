"""Agent spend for the project status summary: integer US cents per member and day.

The one definition of project spend. It reads ``session_daily_cost``, which every
priced turn writes as it happens, and attributes each row through its root
session, so a sub-agent's spend counts for whoever ran the root.

Running sessions count: their spend is included up to their latest priced turn,
on the UTC day each turn happened, not only once the session ends. A session
whose harness reports no usage contributes nothing.

Money is summed exactly in integer micro-dollars and rounded to cents once per
reported figure (the total, each member, each day), halves up, so the total can
differ from the sum of its parts by a cent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.server import session_live_state
from omnigent.server.project_status import (
    UNAVAILABLE,
    MemberKey,
    MemberTotals,
    StatusPeriod,
    member_key_for_session,
    user_member_key,
)
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.stores.conversation_store import ConversationStore


_MICROS_PER_CENT = 10_000


def usd_to_micros(usd: float) -> int:
    """A stored USD amount as integer micro-dollars, so sums are exact."""
    return round(usd * 1_000_000)


def micros_to_cents(micros: int) -> int:
    """Round micro-dollars to the nearest cent, halves up, e.g. ``1_235_000`` -> ``124``."""
    return (micros + _MICROS_PER_CENT // 2) // _MICROS_PER_CENT


def usd_to_cents(usd: float) -> int:
    """Round a USD amount to integer cents, e.g. ``1.234`` -> ``123``."""
    return micros_to_cents(usd_to_micros(usd))


@dataclass(frozen=True)
class SpendBreakdown:
    """A project's window spend in cents.

    :param total: All spend in the window.
    :param by_member: Spend per member key.
    :param by_day: Spend per UTC day ``"YYYY-MM-DD"``.
    :param by_member_day: Spend per member key, then per UTC day.
    """

    total: int
    by_member: dict[MemberKey, int]
    by_day: dict[str, int]
    by_member_day: dict[MemberKey, dict[str, int]]


class ProjectSpend:
    """The project status spend source, also usable directly by other metrics."""

    def __init__(self, conversations: ConversationStore | None = None) -> None:
        self._given = conversations

    @property
    def _conversations(self) -> ConversationStore | None:
        # The server wires its store after sources are built, so resolve it per call.
        return self._given or session_live_state.conversation_store()

    def spend(self, project: Project, period: StatusPeriod) -> MemberTotals:
        """The window's spend total and per-member split in cents, or ``UNAVAILABLE``."""
        breakdown = self.breakdown(project, period)
        if breakdown is None:
            return UNAVAILABLE
        return MemberTotals(total=breakdown.total, by_member=breakdown.by_member)

    def breakdown(self, project: Project, period: StatusPeriod) -> SpendBreakdown | None:
        """The window's spend by member and by UTC day in cents; ``None`` without a store."""
        conversations = self._conversations
        if conversations is None:
            return None
        rows = conversations.list_project_root_daily_costs(
            uuid_to_bytes(project.id).hex(), period.since_day_utc
        )
        members: dict[str, MemberKey] = {}
        total = 0
        member_micros: dict[MemberKey, int] = defaultdict(int)
        day_micros: dict[str, int] = defaultdict(int)
        member_day_micros: dict[MemberKey, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for root_id, day_utc, cost_usd in rows:
            if cost_usd <= 0 or not period.contains_day(day_utc):
                continue
            if root_id not in members:
                members[root_id] = _member_for_root(conversations, project, root_id)
            member = members[root_id]
            micros = usd_to_micros(cost_usd)
            total += micros
            member_micros[member] += micros
            day_micros[day_utc] += micros
            member_day_micros[member][day_utc] += micros
        return SpendBreakdown(
            total=micros_to_cents(total),
            by_member={key: micros_to_cents(value) for key, value in member_micros.items()},
            by_day={day: micros_to_cents(value) for day, value in sorted(day_micros.items())},
            by_member_day={
                key: {day: micros_to_cents(value) for day, value in sorted(days.items())}
                for key, days in member_day_micros.items()
            },
        )


def _member_for_root(
    conversations: ConversationStore, project: Project, root_id: str
) -> MemberKey:
    root = conversations.get_conversation(root_id)
    if root is None:
        # A deleted root's spend still belongs to the project; count it for the owner.
        return user_member_key(project.user_id)
    owner = conversations.get_session_owner_authority(root_id, owner_only=True)
    return member_key_for_session(
        project, root, owner_user_id=owner.user_id if owner is not None else None
    )


def build_source(app_state: Any) -> ProjectSpend:
    """The status summary's spend source; its ``spend`` is ``UNAVAILABLE`` without a store."""
    return ProjectSpend(getattr(app_state, "conversation_store", None))
