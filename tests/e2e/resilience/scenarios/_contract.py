"""Phases and UX-contract checks shared by every scenario script.

A scenario puts a session into a phase, applies its fault, then calls
:func:`finish` to record the checks from ``docs/network-resilience.md``:
interrupted work completes exactly once, a pending approval survives, the
next turn works, the status settles, and no failure shows within the grace.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import pytest

from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from tests.e2e.resilience.lab.driver import ClaudeDriver, Turn
from tests.e2e.resilience.lab.lab import Lab, wait_for
from tests.e2e.resilience.lab.observe import SessionWatcher
from tests.e2e.resilience.lab.report import ScenarioReport

#: Long outages run only with ``OMNIGENT_E2E_RESILIENCE_FULL=1``.
FULL = os.environ.get("OMNIGENT_E2E_RESILIENCE_FULL") == "1"
GRACE_S = float(RUNNER_LIVENESS_TTL_S)
#: Time after recovery for reconnects and retried deliveries to settle.
SETTLE_S = 15.0
#: Upper bound for interrupted work and the next turn to finish once reachable.
RECOVERY_S = 90.0

IDLE = "idle"
TOOL_RUNNING = "tool_running"
TOOL_ENDS_DURING_OUTAGE = "tool_ends_during_outage"
APPROVAL_PENDING = "approval_pending"


def outages(short: Iterable[int], full: Iterable[int]) -> list[int]:
    """Outage lengths for this run: *short* always, *full* only in full mode."""
    return [*short, *(full if FULL else ())]


def cases(
    phases: Iterable[str],
    outage_s: Iterable[int],
    known_gaps: Mapping[tuple[str, int], str] | None = None,
) -> list[object]:
    """Parametrize ``(phase, outage_s)``; known gaps become strict xfails.

    :param phases: Phase names, e.g. ``[IDLE, APPROVAL_PENDING]``.
    :param outage_s: Outage lengths in seconds.
    :param known_gaps: ``(phase, outage) -> finding`` for rows that fail today.
    :returns: ``pytest.param`` entries with ids like ``approval_pending-60s``.
    """
    params = []
    for phase in phases:
        for seconds in outage_s:
            gap = (known_gaps or {}).get((phase, seconds))
            marks = [pytest.mark.xfail(strict=True, reason=gap)] if gap else []
            params.append(pytest.param(phase, seconds, marks=marks, id=f"{phase}-{seconds}s"))
    return params


@dataclass
class Phase:
    """The work in flight when a fault starts.

    :param name: Phase name.
    :param turn: The interrupted turn, or ``None`` when idle.
    :param approval_id: The pending approval's id for ``approval_pending``.
    """

    name: str
    turn: Turn | None = None
    approval_id: str | None = None


def enter(driver: ClaudeDriver, phase: str, *, outage_s: float) -> Phase:
    """Complete one turn, then start the work *phase* names.

    A running tool outlasts the outage by 20 s; one that ends during the outage
    finishes 3 s before it does.

    :param driver: Session driver.
    :param phase: Phase name.
    :param outage_s: Planned outage, used to size tool runtimes.
    :returns: The entered phase.
    """
    driver.round_trip()
    if phase == TOOL_RUNNING:
        return Phase(phase, driver.start_tool_turn(outage_s + 20))
    if phase == TOOL_ENDS_DURING_OUTAGE:
        return Phase(phase, driver.start_tool_turn(max(1.0, outage_s - 3)))
    if phase == APPROVAL_PENDING:
        turn, approval_id = driver.start_approval_turn()
        return Phase(phase, turn, approval_id)
    if phase != IDLE:
        raise ValueError(f"unknown phase {phase!r}")
    return Phase(phase)


def finish(
    report: ScenarioReport,
    lab: Lab,
    driver: ClaudeDriver,
    watcher: SessionWatcher,
    phase: Phase,
    *,
    fault_start: float,
    fault_end: float,
    outage_s: float,
    grace_applies: bool = True,
) -> None:
    """Record every contract check once the fault has cleared.

    :param report: Report collecting the checks.
    :param lab: Running lab.
    :param driver: Session driver.
    :param watcher: Session timeline recorder.
    :param phase: The phase entered before the fault.
    :param fault_start: Wall-clock start of the fault.
    :param fault_end: Wall-clock end of the fault.
    :param outage_s: Planned outage, compared against the reconnect grace.
    :param grace_applies: Whether "no failure within the grace" is expected.
    """
    turn = phase.turn
    if turn is not None and phase.approval_id is not None:
        survived = eventually(lambda: driver.pending_approval(turn), timeout=SETTLE_S)
        report.check(
            "approval_prompt_survives",
            survived is not None,
            "" if survived else f"no pending approval {SETTLE_S:g}s after recovery",
        )
        # A user whose card stayed on screen answers it either way.
        pending = survived or {"elicitation_id": phase.approval_id}
        response = driver.approve(str(pending["elicitation_id"]))
        report.check(
            "approval_request_accepted", response.status_code < 400, f"HTTP {response.status_code}"
        )
    if turn is not None:
        check_turn_completes(report, driver, turn)
    next_turn = eventually_round_trip(driver)
    report.check(
        "next_turn_round_trips",
        next_turn is not None,
        "" if next_turn else "a new message after recovery got no reply",
    )
    check_settled_idle(report, lab, driver.session_id)
    if grace_applies and outage_s < GRACE_S:
        check_no_failure(report, watcher, fault_start, fault_end + SETTLE_S)


def check_turn_completes(report: ScenarioReport, driver: ClaudeDriver, turn: Turn) -> None:
    """The interrupted turn finishes, with every item and side effect exactly once."""
    try:
        driver.wait_done(turn, timeout=RECOVERY_S)
        finished = True
    except TimeoutError:
        finished = False
    report.check("interrupted_turn_finishes", finished, "" if finished else "no final reply")
    user = driver.count_text(turn.marker, role="user")
    report.check("user_message_committed_once", user == 1, f"{user} copies")
    replies = driver.count_text(turn.reply, role="assistant")
    report.check("final_reply_committed_once", replies == 1, f"{replies} copies")
    if turn.done is not None:
        outputs = driver.count_tool_outputs(turn)
        report.check("tool_result_committed_once", outputs == 1, f"{outputs} copies")
        ran = turn.done.exists()
        report.check("tool_side_effect_happened", ran, "" if ran else f"{turn.done.name} missing")


def check_settled_idle(report: ScenarioReport, lab: Lab, session_id: str) -> None:
    """The session settles to ``idle`` once its work is done."""
    status = eventually(
        lambda: "idle" if lab.snapshot(session_id).get("status") == "idle" else None,
        timeout=SETTLE_S,
    )
    report.check(
        "status_settles_idle",
        status == "idle",
        "" if status else f"status={lab.snapshot(session_id).get('status')}",
    )


def check_no_failure(
    report: ScenarioReport, watcher: SessionWatcher, start: float, end: float
) -> None:
    """No ``failed`` status was published or polled in ``[start, end]``."""
    failed = [obs for obs in watcher.statuses(start, end) if obs.status == "failed"]
    report.check(
        "no_failed_status_within_grace",
        not failed,
        f"{len(failed)} failed observation(s), first error={failed[0].error_code}"
        if failed
        else "",
    )


def eventually(predicate: Callable[[], object | None], *, timeout: float) -> object | None:
    """Return the first non-``None`` value of *predicate*, or ``None`` on timeout."""
    try:
        return wait_for(predicate, timeout=timeout, what="the condition")
    except TimeoutError:
        return None


def eventually_round_trip(driver: ClaudeDriver) -> Turn | None:
    """Run a fresh turn; ``None`` when it does not complete in time."""
    try:
        return driver.round_trip(timeout=RECOVERY_S)
    except (AssertionError, TimeoutError):
        return None
