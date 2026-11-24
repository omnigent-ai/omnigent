"""Project status summary: one contract for the window, members and each metric's source.

Every number on a project's Summary page comes from here, so they agree:

- **Time zone and "today":** UTC. ``today_utc`` is the UTC calendar day at request
  time. A window of N days is ``today_utc`` plus the N-1 UTC days before it,
  starting at 00:00:00 UTC, the same rule as the project cost readout.
  ``"all"`` has no lower bound.
- **Members:** the project's people (owner and shared members) and its agent
  identities (the Lead and personas), side by side. Agent sessions that belong
  to no identity, such as ad-hoc workers, count as one ``agents:workers`` member.
- **Money:** integer US cents. Sum exactly, then round each reported figure
  once, half up ($0.105 is 11 cents); never round a float sum with ``round``.

Spend and PRs merged come from pluggable sources. Each lives in its own module
(:data:`SPEND_SOURCE_MODULE`, :data:`PRS_MERGED_SOURCE_MODULE`) exposing
``build_source(app_state)``; until a module exists the summary reports that
metric as unavailable (``None``).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.entities.project_routing import LEAD_IDENTITY_ID, LEAD_NAME, lead_present

if TYPE_CHECKING:
    from omnigent.entities import Conversation, Project
    from omnigent.entities.project import ProjectIdentity

StatusWindow = Literal["7d", "30d", "all"]
STATUS_WINDOWS: tuple[StatusWindow, ...] = ("7d", "30d", "all")
_WINDOW_DAYS: dict[str, int | None] = {"7d": 7, "30d": 30, "all": None}
STATUS_TIME_ZONE = "UTC"

# Member keys: "user:<user_id>", "identity:<identity_id>" (the Lead is
# "identity:orchestrator") or the one pooled key for unaffiliated agent sessions.
MemberKey = str
WORKERS_MEMBER_KEY: MemberKey = "agents:workers"
LEAD_MEMBER_KEY: MemberKey = f"identity:{LEAD_IDENTITY_ID}"

SPEND_SOURCE_MODULE = "omnigent.server.project_status_spend"
PRS_MERGED_SOURCE_MODULE = "omnigent.server.project_status_prs"


def user_member_key(user_id: str | None) -> MemberKey:
    """A person's member key; single-user mode's owner is ``"user:local"``."""
    return f"user:{user_id or 'local'}"


def identity_member_key(identity_id: str) -> MemberKey:
    """An agent identity's member key, e.g. ``"identity:orchestrator"`` for the Lead."""
    return f"identity:{identity_id}"


@dataclass(frozen=True)
class StatusPeriod:
    """The window every metric is measured over.

    :param window: ``"7d"``, ``"30d"`` or ``"all"``.
    :param today_utc: The UTC calendar day at request time, ``"YYYY-MM-DD"``.
    :param since_day_utc: The window's inclusive first UTC day, or ``None`` for ``"all"``.
    :param since_epoch: 00:00:00 UTC of ``since_day_utc``, or ``None`` for ``"all"``.
    :param until_epoch: Request time; nothing later exists yet.
    """

    window: StatusWindow
    today_utc: str
    since_day_utc: str | None
    since_epoch: int | None
    until_epoch: int

    def contains(self, epoch: int) -> bool:
        """Whether an event at *epoch* (Unix seconds) falls in the window."""
        return (
            self.since_epoch is None or epoch >= self.since_epoch
        ) and epoch <= self.until_epoch

    def contains_day(self, day_utc: str) -> bool:
        """Whether a UTC day ``"YYYY-MM-DD"`` falls in the window."""
        return (self.since_day_utc is None or day_utc >= self.since_day_utc) and (
            day_utc <= self.today_utc
        )


def status_period(window: StatusWindow, now_epoch: int) -> StatusPeriod:
    """Resolve *window* at *now_epoch* into its UTC day bounds.

    :raises ValueError: for a window other than ``"7d"``, ``"30d"`` or ``"all"``.
    """
    if window not in _WINDOW_DAYS:
        raise ValueError(f"unknown status window {window!r}")
    today = datetime.fromtimestamp(now_epoch, tz=UTC).date()
    days = _WINDOW_DAYS[window]
    if days is None:
        return StatusPeriod(window, today.isoformat(), None, None, now_epoch)
    since = today - timedelta(days=days - 1)
    since_epoch = int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp())
    return StatusPeriod(window, today.isoformat(), since.isoformat(), since_epoch, now_epoch)


def member_key_for_session(
    project: Project, session: Conversation, *, owner_user_id: str | None = None
) -> MemberKey:
    """Attribute a top-level project session to a member.

    Pass a sub-agent's root session, so its work counts for whoever ran the root.
    The Lead's current and former sessions are the Lead; a session started for an
    identity is that identity; another agent's session (one in a session family)
    is the workers pool; anything else is the person who owns it, *owner_user_id*,
    else the project owner. Every source and the ticket counts use this, so a
    session counts for exactly one member.
    """
    if session.id == project.main_session_id or session.id in project.retired_main_session_ids:
        return LEAD_MEMBER_KEY
    identity_id = (session.labels or {}).get(PROJECT_IDENTITY_LABEL_KEY)
    if identity_id:
        return identity_member_key(identity_id)
    if session.family_id is not None and session.family_id != session.id:
        return WORKERS_MEMBER_KEY
    return user_member_key(owner_user_id or project.user_id)


@dataclass(frozen=True)
class MemberTotals:
    """One metric's window total and per-member split.

    :param total: The project total, e.g. spend in cents or PRs merged.
    :param by_member: Totals per :data:`MemberKey`; may sum to less than
        ``total`` when some of it can't be attributed.
    :param available: ``False`` when the source isn't installed or failed; the
        summary then reports the metric as ``None`` rather than ``0``.
    """

    total: int = 0
    by_member: dict[MemberKey, int] = field(default_factory=dict)
    available: bool = True


UNAVAILABLE = MemberTotals(available=False)


class SpendSource(Protocol):
    """Agent spend in the window, in integer US cents, per member."""

    def spend(self, project: Project, period: StatusPeriod) -> MemberTotals: ...


class PrsMergedSource(Protocol):
    """Pull requests merged in the window, counted per member."""

    def prs_merged(self, project: Project, period: StatusPeriod) -> MemberTotals: ...


@dataclass(frozen=True)
class StatusSources:
    spend: SpendSource | None
    prs_merged: PrsMergedSource | None


def load_status_sources(app_state: Any) -> StatusSources:
    """Find each source's module when it's installed; a missing one stays ``None``."""

    def load(module_name: str) -> Any:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            return None
        return module.build_source(app_state)

    return StatusSources(load(SPEND_SOURCE_MODULE), load(PRS_MERGED_SOURCE_MODULE))


def _cents_per_pr(spend: int | None, prs: int | None) -> int | None:
    """Spend per merged PR, rounded half up like every other cents figure."""
    if spend is None or not prs:
        return None
    return (2 * spend + prs) // (2 * prs)


def summary_members(
    project: Project,
    identities: Iterable[ProjectIdentity],
    member_user_ids: Iterable[str],
    seen: Iterable[MemberKey],
) -> list[dict[str, Any]]:
    """The roster in display order: people, the Lead, personas, then the workers pool.

    Keys seen in the data but no longer on the roster (a former member, a
    deleted identity) are kept so the window's numbers still add up.
    """
    identities = list(identities)
    rows: dict[MemberKey, dict[str, Any]] = {}
    for user_id in dict.fromkeys([project.user_id or "local", *member_user_ids]):
        rows[user_member_key(user_id)] = {
            "kind": "human",
            "name": user_id,
            "user_id": user_id,
            "identity_id": None,
        }
    if lead_present(project, identities):
        rows[LEAD_MEMBER_KEY] = {
            "kind": "identity",
            "name": LEAD_NAME,
            "user_id": None,
            "identity_id": LEAD_IDENTITY_ID,
        }
    for identity in identities:
        rows[identity_member_key(identity.id)] = {
            "kind": "identity",
            "name": identity.name,
            "user_id": None,
            "identity_id": identity.id,
        }
    rows[WORKERS_MEMBER_KEY] = {
        "kind": "agents",
        "name": "Other agents",
        "user_id": None,
        "identity_id": None,
    }
    for key in seen:
        if key in rows:
            continue
        kind, _, rest = key.partition(":")
        rows[key] = {
            "kind": "human" if kind == "user" else "identity",
            "name": rest,
            "user_id": rest if kind == "user" else None,
            "identity_id": rest if kind == "identity" else None,
        }
    return [{"key": key, **row} for key, row in rows.items()]


def build_summary(
    project: Project,
    period: StatusPeriod,
    *,
    roster: list[dict[str, Any]],
    tickets_done: MemberTotals,
    spend: MemberTotals,
    prs_merged: MemberTotals,
) -> dict[str, Any]:
    """Assemble the ``ProjectStatusSummary`` response from each metric."""
    spend_total = spend.total if spend.available else None
    prs_total = prs_merged.total if prs_merged.available else None
    members = []
    for row in roster:
        key = row["key"]
        member_spend = spend.by_member.get(key, 0) if spend.available else None
        member_prs = prs_merged.by_member.get(key, 0) if prs_merged.available else None
        members.append(
            {
                **row,
                "tickets_done": tickets_done.by_member.get(key, 0),
                "prs_merged": member_prs,
                "spend_cents": member_spend,
                "cost_per_merged_pr_cents": _cents_per_pr(member_spend, member_prs),
            }
        )
    return {
        "object": "project.status_summary",
        "project_id": project.id,
        "window": period.window,
        "time_zone": STATUS_TIME_ZONE,
        "today_utc": period.today_utc,
        "since_day_utc": period.since_day_utc,
        "generated_at": period.until_epoch,
        "totals": {
            "tickets_done": tickets_done.total,
            "prs_merged": prs_total,
            "spend_cents": spend_total,
            "cost_per_merged_pr_cents": _cents_per_pr(spend_total, prs_total),
            "members": len(members),
        },
        "members": members,
        "sources": {
            "spend": "available" if spend.available else "unavailable",
            "prs_merged": "available" if prs_merged.available else "unavailable",
        },
    }
