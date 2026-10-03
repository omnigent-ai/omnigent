"""S1: the ingress recycles long-lived connections and caps held requests.

Databricks Apps closes long-lived streams every few minutes and answers 504 to
a request held past its cap. The lab compresses both. Browser connections are
recycled every 8 s, and host and runner connections once they outlive the
reconnect grace. A request left waiting ``_SEVER_S`` for a response gets a
504. Contract: none of this is visible. A pending approval card stays on
screen, a running tool finishes once, and no failure is shown.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import ClaudeDriver
from tests.e2e.resilience.lab.lab import Lab
from tests.e2e.resilience.lab.observe import SessionWatcher
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

# Above the permission hook's 10s held-poll floor, so a sever reads as a proxy
# cap rather than a crash-looping server, as the 300s production cap does.
_SEVER_S = 15.0
# Browser streams recycle often. Host and runner tunnels recycle on the ingress's
# minutes-long lifetime, which stays longer than the reconnect grace.
_CLIENT_RECYCLE_S = 8.0
_HOST_RECYCLE_S = contract.GRACE_S + 10
_PHASES = [contract.TOOL_RUNNING, contract.APPROVAL_PENDING]


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    ("phase", "outage_s"), contract.cases(_PHASES, contract.outages([40], [180]))
)
def test_s1_ingress_recycle(lab_factory: Callable[..., Lab], phase: str, outage_s: int) -> None:
    lab = lab_factory()
    session_id = lab.create_claude_session()
    driver = ClaudeDriver(lab, session_id)
    report = ScenarioReport(
        "S1 ingress recycle",
        {"phase": phase, "window_s": outage_s},
    )
    with SessionWatcher(lab.server_url, session_id) as watcher:
        entered = contract.enter(driver, phase, outage_s=outage_s)
        started = time.time()
        faults = [
            lab.proxies.client.recycle(_CLIENT_RECYCLE_S),
            lab.proxies.host.recycle(_HOST_RECYCLE_S),
            lab.proxies.client.sever_held(_SEVER_S),
            lab.proxies.host.sever_held(_SEVER_S),
        ]
        try:
            time.sleep(outage_s)
        finally:
            for fault in faults:
                fault.clear()
        ended = time.time()
        reasons = [e.fields.get("reason") for e in lab.events.events(kind="disconnect")]
        recycled, severed = reasons.count("recycle"), reasons.count("sever_held")
        report.check(
            "ingress_faults_fired", recycled > 0, f"recycled={recycled} severed={severed}"
        )
        if phase == contract.APPROVAL_PENDING:
            gaps = [o for o in watcher.between(started, ended) if o.pending_approvals == 0]
            report.check(
                "approval_card_never_disappears",
                not gaps,
                f"{len(gaps)} snapshot(s) without the card" if gaps else "",
            )
        contract.finish(
            report,
            lab,
            driver,
            watcher,
            entered,
            fault_start=started,
            fault_end=ended,
            outage_s=0,
        )
    report.attach(watcher, lab.root)
    report.require()
