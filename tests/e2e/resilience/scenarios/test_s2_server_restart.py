"""S2: the server restarts (a deploy) while a claude-native session is in each phase.

The host, runner and Claude Code keep running; only the server and every link
to it go away. Contract: within the reconnect grace the user never sees a
failure; afterwards the committed transcript, tool side effects and status
match an uninterrupted run, a pending approval survives, and the next turn works.

Short outages run by default. Set ``OMNIGENT_E2E_RESILIENCE_FULL=1`` for the
long ones, including past the grace.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import pytest

from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from tests.e2e.resilience.lab.driver import ClaudeDriver, Turn
from tests.e2e.resilience.lab.lab import Lab, wait_for
from tests.e2e.resilience.lab.observe import SessionWatcher
from tests.e2e.resilience.lab.report import ScenarioReport

_FULL = os.environ.get("OMNIGENT_E2E_RESILIENCE_FULL") == "1"
_OUTAGES_S = [5, 60, 120] if _FULL else [5]
_GRACE_S = float(RUNNER_LIVENESS_TTL_S)
# Time after the server returns for reconnects and retried deliveries to settle.
_SETTLE_S = 15.0
# Upper bound for interrupted work and the next turn to finish once reachable.
_RECOVERY_S = 90.0
_PHASES = ["idle", "tool_running", "tool_ends_during_outage", "approval_pending"]
# Rows that break the contract today, keyed by (phase, outage), with the plan's
# hypothesis id. Strict, so a fix that makes one pass flags its stale entry.
_KNOWN_GAPS = {
    ("approval_pending", 60): (
        "H5: the permission hook retries at most every 30s, so the card is gone for up "
        "to 30s after the server returns and an approval in that gap is lost"
    ),
    ("approval_pending", 120): (
        "H5: after 8 consecutive failed re-POSTs (~90s) the permission hook falls back "
        "to the terminal prompt; the web card never returns and the turn stays blocked"
    ),
}


def _cases() -> list[object]:
    cases = []
    for phase in _PHASES:
        for outage_s in _OUTAGES_S:
            gap = _KNOWN_GAPS.get((phase, outage_s))
            marks = [pytest.mark.xfail(strict=True, reason=gap)] if gap else []
            cases.append(pytest.param(phase, outage_s, marks=marks, id=f"{phase}-{outage_s}s"))
    return cases


@pytest.mark.timeout(600)
@pytest.mark.parametrize(("phase", "outage_s"), _cases())
def test_s2_server_restart(lab_factory: Callable[..., Lab], phase: str, outage_s: int) -> None:
    lab = lab_factory()
    session_id = lab.create_claude_session()
    driver = ClaudeDriver(lab, session_id)
    report = ScenarioReport("S2 server restart", {"phase": phase, "outage_s": outage_s})
    with SessionWatcher(lab.server_url, session_id) as watcher:
        driver.round_trip()
        turn: Turn | None = None
        approval_id: str | None = None
        if phase == "tool_running":
            turn = driver.start_tool_turn(outage_s + 20)
        elif phase == "tool_ends_during_outage":
            turn = driver.start_tool_turn(max(1, outage_s - 3))
        elif phase == "approval_pending":
            turn, approval_id = driver.start_approval_turn()

        down_at = time.time()
        lab.restart_server(downtime_s=outage_s)
        up_at = time.time()

        if turn is not None and approval_id is not None:
            survived = _eventually(lambda: driver.pending_approval(turn), timeout=_SETTLE_S)
            report.check(
                "approval_prompt_survives_restart",
                survived is not None,
                "" if survived else "no pending approval after the server returned",
            )
            pending = survived or {"elicitation_id": approval_id}
            response = driver.approve(str(pending["elicitation_id"]))
            report.check(
                "approval_request_accepted",
                response.status_code < 400,
                f"HTTP {response.status_code}",
            )

        if turn is not None:
            _check_turn_completes(report, driver, turn)
        next_turn = _eventually_round_trip(driver)
        report.check(
            "next_turn_round_trips",
            next_turn is not None,
            "" if next_turn else "a new message after recovery got no reply",
        )
        _check_settled_idle(report, lab, session_id)
        settled_at = time.time()

    if outage_s < _GRACE_S:
        failed = [obs for obs in watcher.statuses(down_at, settled_at) if obs.status == "failed"]
        report.check(
            "no_failed_status_within_grace",
            not failed,
            f"{len(failed)} failed observation(s), first error={failed[0].error_code}"
            if failed
            else "",
        )
    report.check(
        "server_was_down_for_outage",
        up_at - down_at >= outage_s,
        f"{up_at - down_at:.1f}s",
    )
    report.attach(watcher, lab.root)
    report.require()


def _check_turn_completes(report: ScenarioReport, driver: ClaudeDriver, turn: Turn) -> None:
    try:
        driver.wait_done(turn, timeout=_RECOVERY_S)
        finished = True
    except TimeoutError:
        finished = False
    report.check("interrupted_turn_finishes", finished, "" if finished else "no final reply")
    user = driver.count_text(turn.marker, role="user")
    report.check("user_message_committed_once", user == 1, f"{user} copies")
    replies = driver.count_text(turn.reply, role="assistant")
    report.check("final_reply_committed_once", replies == 1, f"{replies} copies")
    outputs = driver.count_tool_outputs(turn)
    report.check("tool_result_committed_once", outputs == 1, f"{outputs} copies")
    ran = turn.done is not None and turn.done.exists()
    report.check("tool_side_effect_happened", ran, "" if ran else f"{turn.done} missing")


def _check_settled_idle(report: ScenarioReport, lab: Lab, session_id: str) -> None:
    status = _eventually(
        lambda: "idle" if lab.snapshot(session_id).get("status") == "idle" else None,
        timeout=_SETTLE_S,
    )
    report.check(
        "status_settles_idle",
        status == "idle",
        "" if status else f"status={lab.snapshot(session_id).get('status')}",
    )


def _eventually(predicate: Callable[[], object | None], *, timeout: float) -> object | None:
    try:
        return wait_for(predicate, timeout=timeout, what="the condition")
    except TimeoutError:
        return None


def _eventually_round_trip(driver: ClaudeDriver) -> Turn | None:
    try:
        return driver.round_trip(timeout=_RECOVERY_S)
    except (AssertionError, TimeoutError):
        return None
