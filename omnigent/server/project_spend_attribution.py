"""Split a project's spend by role and by ticket, for telemetry.

Spend is recorded per root session and UTC day (see ``ProjectSpend.root_days``).

**By role:** each root session's member (``member_key_for_session``) decides its role:
an identity is that role, the Lead is the Lead, and every person's own chats are pooled
as "People's own chats". A helper session with no role of its own counts for whoever
created it, walking ``created_by_session_id`` up to the first session that has one; only
helpers whose chain ends without one stay "Other agents". The rows sum to the window's
spend.

**By ticket:** each root session's spend on a day goes to tickets in this order:

1. Tickets the session (or a sub-agent under it) acted on that day, created, commented,
   moved or edited, weighted by how many events it made on each.
2. Else tickets linked to the session by then that weren't already finished before the
   day began, split evenly.
3. Else it's unattributed.

Ticket rows plus ``unattributed_usd`` sum to the window's spend. Everything is computed from
indexed reads already made for telemetry (spend rows, ticket events, links); nothing is
stored.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from omnigent.server.project_kpis import DAY, StageHistories
from omnigent.server.project_status import LEAD_MEMBER_KEY, WORKERS_MEMBER_KEY, MemberKey
from omnigent.server.project_status_spend import micros_to_cents

#: Ticket rows returned in ``spend_by_ticket``; the rest are summed in ``other_tickets_usd``.
TOP_TICKETS = 10
PEOPLE_ROLE = "people"
SpendRow = tuple[str, str, int, MemberKey]


def _usd(micros: int) -> float:
    return micros_to_cents(micros) / 100


def _day_bounds(day_utc: str) -> tuple[int, int]:
    start = int(datetime.fromisoformat(day_utc).replace(tzinfo=UTC).timestamp())
    return start, start + DAY


def role_key(member: MemberKey) -> str:
    """The role a member's spend counts for: an identity ID, ``people``, or the pool."""
    if member.startswith("user:"):
        return PEOPLE_ROLE
    return member


def spend_by_role(rows: Iterable[SpendRow], role_names: Mapping[str, str]) -> list[dict[str, Any]]:
    """Spend per role, largest first; *role_names* maps identity member keys to names."""
    micros: dict[str, int] = defaultdict(int)
    for _root, _day, amount, member in rows:
        micros[role_key(member)] += amount
    labels = {
        LEAD_MEMBER_KEY: "Lead",
        WORKERS_MEMBER_KEY: "Other agents",
        PEOPLE_ROLE: "People's own chats",
    }
    result = [
        {
            "key": key,
            "name": labels.get(key) or role_names.get(key) or "Removed role",
            "identity_id": key.removeprefix("identity:") if key.startswith("identity:") else None,
            "spend_usd": _usd(amount),
        }
        for key, amount in micros.items()
    ]
    return sorted(result, key=lambda row: (-row["spend_usd"], row["name"]))


#: Longest creator chain followed; deeper (or cyclic) chains stay "Other agents".
MAX_CREATOR_DEPTH = 10


def inherit_creator_roles(
    rows: list[SpendRow],
    creator_of: Callable[[list[str]], dict[str, str]],
    member_of: Callable[[list[str]], dict[str, MemberKey]],
) -> list[SpendRow]:
    """Re-credit "Other agents" rows to the member of the nearest creator that has one.

    :param creator_of: Session IDs to the session that created each, for those that know.
    :param member_of: Session IDs to their member keys.
    """
    pending = {root for root, _, _, member in rows if member == WORKERS_MEMBER_KEY}
    resolved: dict[str, MemberKey] = {}
    current = {root: root for root in pending}
    seen: dict[str, set[str]] = {root: {root} for root in pending}
    for _ in range(MAX_CREATOR_DEPTH):
        if not current:
            break
        creators = creator_of(sorted(set(current.values())))
        nxt = {
            root: creators[at]
            for root, at in current.items()
            if at in creators and creators[at] not in seen[root]
        }
        members = member_of(sorted(set(nxt.values()))) if nxt else {}
        current = {}
        for root, creator in nxt.items():
            seen[root].add(creator)
            member = members.get(creator)
            if member is None:
                continue
            if member == WORKERS_MEMBER_KEY:
                current[root] = creator
            else:
                resolved[root] = member
    return [
        (root, day, micros, resolved.get(root, member) if member == WORKERS_MEMBER_KEY else member)
        for root, day, micros, member in rows
    ]


def finished_at(histories: StageHistories) -> dict[str, int]:
    """When each ticket now in a completed stage last entered it."""
    done: dict[str, int] = {}
    for ticket_id, history in histories.items():
        if history and history[-1]["is_complete"]:
            done[ticket_id] = history[-1]["at"]
    return done


def _split(micros: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Whole micro-dollars in proportion to *weights*; the remainder goes to the heaviest."""
    total = sum(weights.values())
    shares = {key: micros * weight // total for key, weight in weights.items()}
    heaviest = max(weights, key=lambda key: (weights[key], key))
    shares[heaviest] += micros - sum(shares.values())
    return shares


def spend_by_ticket(
    rows: Iterable[SpendRow],
    root_of: Mapping[str, str],
    activity: Iterable[tuple[str, str, int]],
    links: Iterable[tuple[str, str, int]],
    finished: Mapping[str, int],
    titles: Mapping[str, str | None],
    top: int = TOP_TICKETS,
) -> dict[str, Any]:
    """Spend per ticket (see the module docstring), the top *top* tickets first.

    :param root_of: Session ID to its root session ID; unknown sessions are their own root.
    :param activity: ``(ticket_id, session_id, at)`` for each ticket event a session made.
    :param links: ``(ticket_id, session_id, linked_at)`` for each ticket-session link.
    :param finished: When each currently finished ticket entered its completed stage.
    """
    acted: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for ticket_id, session_id, at in activity:
        day = datetime.fromtimestamp(at, tz=UTC).date().isoformat()
        acted[(root_of.get(session_id, session_id), day)][ticket_id] += 1
    linked: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for ticket_id, session_id, linked_at in links:
        linked[root_of.get(session_id, session_id)].append((ticket_id, linked_at))
    per_ticket: dict[str, int] = defaultdict(int)
    unattributed = 0
    for root_id, day, micros, _member in rows:
        weights: Mapping[str, int] = acted.get((root_id, day)) or {}
        if not weights:
            start, end = _day_bounds(day)
            open_links = {
                ticket_id
                for ticket_id, linked_at in linked.get(root_id, ())
                if linked_at < end and finished.get(ticket_id, end) >= start
            }
            weights = dict.fromkeys(open_links, 1)
        if not weights:
            unattributed += micros
            continue
        for ticket_id, share in _split(micros, weights).items():
            per_ticket[ticket_id] += share
    ranked = sorted(per_ticket.items(), key=lambda item: (-item[1], item[0]))
    return {
        "tickets": [
            {"ticket_id": ticket_id, "title": titles.get(ticket_id), "spend_usd": _usd(micros)}
            for ticket_id, micros in ranked[:top]
        ],
        "tickets_with_spend": len(ranked),
        "other_tickets_usd": _usd(sum(micros for _, micros in ranked[top:])),
        "attributed_usd": _usd(sum(per_ticket.values())),
        "unattributed_usd": _usd(unattributed),
    }
