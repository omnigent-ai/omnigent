"""When a board card reads as stalled, and who is at work on a running one.

A card is stalled when its ticket sits in a watched working stage with no open
ask for a person, no deliberate ``waiting`` wake-up and no linked worker running,
and neither the ticket nor those workers have changed for the card's threshold.
The stages and thresholds follow the reliability loop's (see
:mod:`omnigent.server.project_reliability`), but a card waits at least
:data:`CARD_STALL_MIN_S`, past the loop's own nudge, restart and escalation, so
it only flags work the automatic checks didn't get moving. The router's sessions
(the Lead's) don't count as the ticket's workers: they hear about every ticket.

Work that waits on something gets a quiet waiting state instead, and no stall
clock (see :func:`ticket_wait`): a person's answer, a sign-off label on approved
work, landing once fully approved and verified, or a ``waiting`` wake-up.

The server sends the idle start and the threshold; the board compares them with
the clock, so a card turns stalled without another request.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omnigent.server.project_reliability import (
    WAITING_LABEL,
    Settings,
    ship_stage_id,
)

#: A card never reads as stalled sooner than this, whatever the stage's loop threshold.
CARD_STALL_MIN_S = 2 * 3600
#: How long a card may wait before it reads as waiting too long, by kind. A
#: ``waiting`` wake-up has its own time, so it never ages.
LONG_WAIT_S = {"land": 24 * 3600, "sign_off": 48 * 3600, "person": 48 * 3600}
_NEEDS_HUMAN_LABEL = "needs-human"


@dataclass(frozen=True)
class BoardStall:
    """What a page of cards needs to judge stalls.

    :param stages: The project's stages, in board order.
    :param settings: Its reliability settings.
    :param router_session_ids: The router's sessions, which aren't any ticket's worker.
    :param project: The project, for which sign-offs its tickets need.
    """

    stages: Sequence[Mapping[str, Any]]
    settings: Settings
    router_session_ids: frozenset[str]
    project: Any = None


@dataclass(frozen=True)
class WorkerState:
    """One linked session, as the board sees it.

    :param status: Its live status: ``"running"``, ``"waiting"``, ``"idle"`` or
        ``"failed"``, or ``None`` when unknown.
    :param updated_at: Epoch seconds the session last changed.
    :param name: Who it works as, e.g. ``"Platform Engineer"``.
    :param running_since: Epoch seconds its current run began, when known.
    """

    status: str | None
    updated_at: int
    name: str
    running_since: float | None = None


def in_working_stage(stages: Sequence[Mapping[str, Any]], stage_id: str) -> bool:
    """Whether *stage_id* is started and unfinished: neither the first stage nor the last."""
    return stage_id in [stage["id"] for stage in stages[1:-1]]


def card_stall_after(
    stages: Sequence[Mapping[str, Any]], stage_id: str, config: Settings
) -> float | None:
    """Seconds without activity before a card in *stage_id* reads as stalled.

    ``None`` for stages the loop doesn't watch: the first (not started), the
    completed one and any whose ``stage_minutes`` is 0. The stage holding
    finished work waiting to ship uses the loop's ship reminder wait instead.
    """
    if not in_working_stage(stages, stage_id) or stage_id in config.unwatched_stages:
        return None
    if stage_id == ship_stage_id(list(stages)):
        return max(CARD_STALL_MIN_S, config.ship_wait_s)
    return max(CARD_STALL_MIN_S, config.threshold(stage_id))


def ticket_stall(
    ticket: Mapping[str, Any],
    after_s: float | None,
    workers: Collection[WorkerState],
    *,
    workers_known: bool,
) -> dict[str, int] | None:
    """The card's stall clock, ``{"idle_since", "after_s"}``, or ``None`` when it can't stall.

    :param ticket: The ticket as listed, with ``labels``, ``waiting_on`` and ``updated_at``.
    :param after_s: Its stage's threshold from :func:`card_stall_after`.
    :param workers: Its linked sessions, without the router's.
    :param workers_known: Whether every linked session was read; when not, a
        worker might be running, so the card doesn't claim a stall.
    """
    if after_s is None or not workers_known or ticket.get("waiting_on"):
        return None
    keys = {label.get("key") for label in ticket.get("labels") or []}
    if _NEEDS_HUMAN_LABEL in keys or WAITING_LABEL in keys:
        return None
    # A waiting worker is blocked on an approval or its background agents, not idle.
    if any(worker.status not in ("idle", "failed") for worker in workers):
        return None
    idle_since = max([int(ticket.get("updated_at") or 0), *(w.updated_at for w in workers)])
    return {"idle_since": idle_since, "after_s": int(after_s)}


def missing_sign_offs(project: Any, ticket: Mapping[str, Any]) -> list[str]:
    """The sign-off labels the ticket still needs before it can land, e.g. ``["risk-ok"]``."""
    from omnigent.server.github_pr_intake import is_outside_requester
    from omnigent.server.product_approval import (
        OWNER_OK_LABEL,
        PRODUCT_OK_LABEL,
        RISK_LABEL,
        RISK_OK_LABEL,
        needs_product_ok,
    )

    keys = {label.get("key") for label in ticket.get("labels") or []}
    missing = []
    if RISK_LABEL in keys and RISK_OK_LABEL not in keys:
        missing.append(RISK_OK_LABEL)
    if project is not None and needs_product_ok(project, dict(ticket)):
        if PRODUCT_OK_LABEL not in keys:
            missing.append(PRODUCT_OK_LABEL)
    if is_outside_requester(ticket.get("requested_by")) and OWNER_OK_LABEL not in keys:
        missing.append(OWNER_OK_LABEL)
    return missing


def ticket_wait(ticket: Mapping[str, Any], project: Any) -> dict[str, Any] | None:
    """What a card waits on, ``{"kind", "on"}``, or ``None`` when nothing is awaited.

    Kinds, first match wins:

    - ``person``: an open ``needs-human`` ask; the board names its responders.
    - ``until``: a ``waiting`` wake-up; ``on`` is its value, ``"<ISO time> <note>"``.
    - ``sign_off``: Reviewer-approved work missing sign-off labels; ``on`` lists them.
    - ``land``: approved and verified at the same commit, with every sign-off in:
      it waits its turn to land, often behind other tickets.
    """
    values = {label.get("key"): label.get("value") for label in ticket.get("labels") or []}
    if ticket.get("waiting_on") or _NEEDS_HUMAN_LABEL in values:
        return {"kind": "person", "on": []}
    if WAITING_LABEL in values:
        return {"kind": "until", "on": [values[WAITING_LABEL] or ""]}
    approved = values.get("approved")
    if not approved:
        return None
    missing = missing_sign_offs(project, ticket)
    if missing:
        return {"kind": "sign_off", "on": missing}
    if values.get("verified") == approved:
        return {"kind": "land", "on": []}
    return None


def wait_clock(
    wait: dict[str, Any], ticket: Mapping[str, Any], workers: Collection[WorkerState]
) -> dict[str, Any]:
    """Add ``since`` and ``after_s`` to *wait*, so the board can tell when it ran too long.

    ``since`` is when the ask was raised, for a person, else the latest change to the
    ticket or its workers; ``after_s`` is ``None`` for kinds that never age.
    """
    raised = (
        (ticket.get("waiting_on") or {}).get("raised_at") if wait["kind"] == "person" else None
    )
    since = raised or max([int(ticket.get("updated_at") or 0), *(w.updated_at for w in workers)])
    return {**wait, "since": int(since), "after_s": LONG_WAIT_S.get(wait["kind"])}


def ticket_working(workers: Sequence[WorkerState]) -> dict[str, Any] | None:
    """The first running worker, ``{"name", "since"}``; ``since`` is ``None`` when unknown."""
    for worker in workers:
        if worker.status == "running":
            since = int(worker.running_since) if worker.running_since is not None else None
            return {"name": worker.name, "since": since}
    return None
