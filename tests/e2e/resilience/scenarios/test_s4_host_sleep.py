"""S4: the host sleeps (laptop lid closed) and wakes while Claude has work in flight.

Every host-side process freezes and the host's links go dark. On wake the
processes resume first and the network follows. Contract: as for any outage
within the grace, nothing visibly fails; past it the session recovers once
the host is back.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import SessionDriver
from tests.e2e.resilience.lab.lab import Harness, Lab
from tests.e2e.resilience.lab.observe import SessionWatcher
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

_PHASES = [contract.IDLE, contract.TOOL_RUNNING, contract.APPROVAL_PENDING]
_WAKE_NETWORK_DELAY_S = 2.0


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    ("harness", "phase", "outage_s"), contract.cases(_PHASES, contract.outages([10], [60, 300]))
)
def test_s4_host_sleep(
    lab_factory: Callable[..., Lab], harness: Harness, phase: str, outage_s: int
) -> None:
    lab = lab_factory()
    driver = SessionDriver.create(lab, harness)
    session_id = driver.session_id
    report = ScenarioReport(
        "S4 host sleep", {"harness": harness, "phase": phase, "outage_s": outage_s}
    )
    with SessionWatcher(lab.server_url, session_id) as watcher:
        entered = contract.enter(driver, phase, outage_s=outage_s)
        started = time.time()
        with lab.sleep_host(wake_network_delay_s=_WAKE_NETWORK_DELAY_S):
            time.sleep(outage_s)
        ended = time.time()
        contract.finish(
            report,
            lab,
            driver,
            watcher,
            entered,
            fault_start=started,
            fault_end=ended,
            outage_s=outage_s,
        )
    report.attach(watcher, lab.root)
    report.require()
