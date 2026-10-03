"""S5: the host is offline while the user, elsewhere, keeps using the session.

The host's links are down (refused, as when the laptop has no network) and the
server and browser are fine. The user sends a message, answers a pending
approval, or stops a running turn. Contract: each action is either applied
exactly once when the host returns, or refused at once so the user knows to
retry. It is never silently dropped or applied twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from tests.e2e.resilience.lab.driver import ClaudeDriver
from tests.e2e.resilience.lab.lab import Lab
from tests.e2e.resilience.lab.observe import SessionWatcher
from tests.e2e.resilience.lab.proxy import Fault
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

_ACTIONS = {
    "send": contract.IDLE,
    "approve": contract.APPROVAL_PENDING,
    "stop": contract.TOOL_RUNNING,
}
_KNOWN_GAPS = {
    ("approve", 150): (
        "R2: the permission hook gives up after ~90s of failed re-POSTs, so an approval "
        "given while the host was offline never reaches Claude"
    ),
    ("send", 150): "R3: the message is accepted, then dropped when the relaunch fails",
    ("stop", 150): "R4: Stop reports success while the turn keeps running on the host",
    ("send", 20): (
        "R3: a message sent while the host is unreachable is accepted (202), then the "
        "relaunch fails with runner_failed_to_start; the message is never delivered and "
        "the failure stays after the host returns"
    ),
    ("stop", 20): (
        "R4: Stop with the host unreachable reports success and shows idle, but the turn "
        "keeps running on the host; the next message is typed into the busy TUI and lost"
    ),
}
#: A refusal slower than this is not "at once" for a user waiting on a button.
_PROMPT_REFUSAL_S = 35.0


def _host_offline(lab: Lab) -> list[Fault]:
    links = (lab.proxies.host, lab.proxies.model)
    faults = [link.refuse() for link in links]
    for link in links:
        link.reset()
    return faults


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    ("action", "outage_s"), contract.cases(_ACTIONS, contract.outages([20], [150]), _KNOWN_GAPS)
)
def test_s5_host_offline_user_acts(
    lab_factory: Callable[..., Lab], action: str, outage_s: int
) -> None:
    lab = lab_factory()
    session_id = lab.create_claude_session()
    driver = ClaudeDriver(lab, session_id)
    report = ScenarioReport("S5 host offline, user acts", {"action": action, "outage_s": outage_s})
    with SessionWatcher(lab.server_url, session_id) as watcher:
        entered = contract.enter(driver, _ACTIONS[action], outage_s=outage_s)
        started = time.time()
        faults = _host_offline(lab)
        try:
            time.sleep(2.0)
            response, elapsed = _act(driver, entered, action)
            accepted = response is not None and response.status_code < 400
            status = response.status_code if response is not None else "timeout"
            report.check(
                "action_accepted_or_refused_promptly",
                accepted or elapsed <= _PROMPT_REFUSAL_S,
                f"HTTP {status} after {elapsed:.1f}s",
            )
            time.sleep(max(0.0, started + outage_s - time.time()))
        finally:
            for fault in faults:
                fault.clear()
        ended = time.time()
        _check_outcome(report, lab, driver, entered, action, accepted)
        contract.check_settled_idle(report, lab, session_id)
        next_turn = contract.eventually_round_trip(driver)
        report.check(
            "next_turn_round_trips",
            next_turn is not None,
            "" if next_turn else "a new message after recovery got no reply",
        )
        if outage_s < contract.GRACE_S:
            contract.check_no_failure(report, watcher, started, ended + contract.SETTLE_S)
    report.attach(watcher, lab.root)
    report.require()


def _act(
    driver: ClaudeDriver, entered: contract.Phase, action: str
) -> tuple[httpx.Response | None, float]:
    started = time.monotonic()
    try:
        if action == "send":
            entered.turn = driver.text_turn()
            response = driver.send_raw(entered.turn, timeout=120)
        elif action == "approve":
            assert entered.approval_id is not None
            response = driver.approve(entered.approval_id)
        else:
            assert driver.lab.client is not None
            response = driver.lab.client.post(
                f"/v1/sessions/{driver.session_id}/events",
                json={"type": "stop_session", "data": {}},
                timeout=120,
            )
    except httpx.HTTPError:
        response = None
    return response, time.monotonic() - started


def _check_outcome(
    report: ScenarioReport,
    lab: Lab,
    driver: ClaudeDriver,
    entered: contract.Phase,
    action: str,
    accepted: bool,
) -> None:
    turn = entered.turn
    assert turn is not None
    if action == "stop":
        stopped = contract.eventually(
            lambda: True if lab.snapshot(driver.session_id).get("status") != "running" else None,
            timeout=contract.RECOVERY_S,
        )
        report.check(
            "stop_takes_effect" if accepted else "refused_stop_leaves_turn_running",
            bool(stopped) if accepted else True,
            f"status={lab.snapshot(driver.session_id).get('status')}",
        )
        return
    if action == "send" and not accepted:
        copies = driver.count_text(turn.marker, role="user")
        report.check("refused_message_not_delivered", copies == 0, f"{copies} copies")
        return
    contract.check_turn_completes(report, driver, turn)
