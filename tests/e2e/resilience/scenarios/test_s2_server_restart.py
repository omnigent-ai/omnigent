"""S2: the server restarts (a deploy) while a claude-native session is in each phase.

The host, runner and Claude Code keep running; only the server and every link
to it go away. Contract: within the reconnect grace the user never sees a
failure; afterwards the committed transcript, tool side effects and status
match an uninterrupted run, a pending approval survives, and the next turn works.

Short outages run by default. Set ``OMNIGENT_E2E_RESILIENCE_FULL=1`` for the
long ones, including past the grace.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import SessionDriver
from tests.e2e.resilience.lab.lab import Harness, Lab
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

_PHASES = [
    contract.IDLE,
    contract.TOOL_RUNNING,
    contract.TOOL_ENDS_DURING_OUTAGE,
    contract.APPROVAL_PENDING,
]
_KNOWN_GAPS = {
    **contract.gaps(
        "R1: the permission hook retries at most every 30s, so the card is gone for up "
        "to 30s after the server returns and an approval in that gap is lost",
        [(contract.APPROVAL_PENDING, 60)],
    ),
    **contract.gaps(
        "R1: Codex re-POSTs its approval at most every 30s, so the card returns up to 30s "
        "after the server and an approval in that gap is lost",
        [(contract.APPROVAL_PENDING, 60), (contract.APPROVAL_PENDING, 120)],
        harnesses=("codex",),
    ),
    **contract.gaps(
        "R2: after 8 consecutive failed re-POSTs (~90s) the permission hook falls back "
        "to the terminal prompt; the web card never returns and the turn stays blocked",
        [(contract.APPROVAL_PENDING, 120)],
    ),
}


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    ("harness", "phase", "outage_s"),
    contract.cases(_PHASES, contract.outages([5], [60, 120]), _KNOWN_GAPS),
)
def test_s2_server_restart(
    lab_factory: Callable[..., Lab], harness: Harness, phase: str, outage_s: int
) -> None:
    lab = lab_factory()
    driver = SessionDriver.create(lab, harness)
    report = ScenarioReport(
        "S2 server restart", {"harness": harness, "phase": phase, "outage_s": outage_s}
    )
    with contract.observe(lab, driver, report) as watcher:
        entered = contract.enter(driver, phase, outage_s=outage_s)
        down_at = time.time()
        lab.restart_server(downtime_s=outage_s)
        up_at = time.time()
        contract.finish(
            report,
            lab,
            driver,
            watcher,
            entered,
            fault_start=down_at,
            fault_end=up_at,
            outage_s=outage_s,
        )
    report.require()
