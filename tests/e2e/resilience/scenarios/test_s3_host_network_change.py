"""S3: the host's network changes (Wi-Fi switch, VPN toggle) while Claude works.

Everything on the host loses the network while its processes keep running:
the daemon, runner, hooks and Claude Code's model calls. ``blackhole`` leaves
connections half-open, as a silently dropped link does. ``reset`` kills them
and refuses new ones, as a downed interface does. ``flap`` drops them for 2 s
every 15 s for the whole window, as a marginal Wi-Fi does; every drop is a
blip, so the session must never show a failure. The server and the user's
browser are unaffected.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import SessionDriver
from tests.e2e.resilience.lab.lab import Harness, Lab
from tests.e2e.resilience.lab.proxy import Fault
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

_PHASES = [contract.IDLE, contract.TOOL_RUNNING, contract.APPROVAL_PENDING]


_FLAP_UP_S = 15.0
_FLAP_DOWN_S = 2.0
_R6 = (
    "R6: drops that each recover within seconds still add up to the 90s relay grace "
    "and flash runner_disconnected over the running turn"
)
_KNOWN_GAPS_BY_MODE = {
    "flap": contract.gaps(
        _R6,
        [(contract.TOOL_RUNNING, 120), (contract.APPROVAL_PENDING, 120)],
        harnesses=("claude", "codex"),
    ),
    "reset": {
        **contract.gaps(
            "R1: refused re-POSTs back off to 30s, so the approval card returns late and an "
            "approval in the gap is lost",
            [(contract.APPROVAL_PENDING, 45)],
        ),
        **contract.gaps(
            "R2: the permission hook gives up after 8 failed re-POSTs and moves the prompt "
            "to the terminal",
            [(contract.APPROVAL_PENDING, 120)],
        ),
    },
}


def _cut(lab: Lab, mode: str) -> list[Fault]:
    links = (lab.proxies.host, lab.proxies.model)
    if mode == "blackhole":
        return [link.blackhole() for link in links]
    if mode == "flap":
        return [link.flap(up_s=_FLAP_UP_S, down_s=_FLAP_DOWN_S, mode="reset") for link in links]
    faults = [link.refuse() for link in links]
    for link in links:
        link.reset()
    return faults


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    ("mode", "harness", "phase", "outage_s"),
    [
        pytest.param(mode, *case.values, marks=case.marks, id=f"{mode}-{case.id}")
        for mode in ("blackhole", "reset", "flap")
        for case in contract.cases(
            _PHASES,
            # Flapping takes a while to mean anything; it runs only in full mode.
            contract.outages([], [40, 120])
            if mode == "flap"
            else contract.outages([10], [45, 120]),
            _KNOWN_GAPS_BY_MODE.get(mode),
        )
    ],
)
def test_s3_host_network_change(
    lab_factory: Callable[..., Lab], mode: str, harness: Harness, phase: str, outage_s: int
) -> None:
    lab = lab_factory()
    driver = SessionDriver.create(lab, harness)
    report = ScenarioReport(
        "S3 host network change",
        {"mode": mode, "harness": harness, "phase": phase, "outage_s": outage_s},
    )
    with contract.observe(lab, driver, report) as watcher:
        entered = contract.enter(driver, phase, outage_s=outage_s)
        started = time.time()
        faults = _cut(lab, mode)
        try:
            time.sleep(outage_s)
        finally:
            for fault in faults:
                fault.clear()
        ended = time.time()
        contract.finish(
            report,
            lab,
            driver,
            watcher,
            entered,
            fault_start=started,
            fault_end=ended,
            outage_s=_FLAP_DOWN_S if mode == "flap" else outage_s,
            racy_gaps={"status_settles_idle": "R8"} if harness == "codex" else None,
        )
    report.require()
