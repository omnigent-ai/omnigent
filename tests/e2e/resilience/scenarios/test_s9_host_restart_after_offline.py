"""S9: the user restarts the host after it went offline mid-turn.

The host's links are refused past the reconnect grace while a tool runs, so
the server holds the turn. The user then quits the host daemon with Ctrl-C,
which ends its runners, and starts it again once the network is back.
Contract: past the grace the page says the host is offline, and once the host
returns without its runner the terminal leaves "Starting up…" for a live
terminal or Resume. Needs Playwright's Chromium and a built web UI.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import SessionDriver
from tests.e2e.resilience.lab.lab import Lab
from tests.e2e.resilience.lab.recorder import show_view
from tests.e2e.resilience.lab.report import ScenarioReport, report_dir
from tests.e2e.resilience.scenarios import _contract as contract

#: Must outlast the grace, so S9 has no short case for ``contract.FULL`` to extend.
_OUTAGE_S = contract.GRACE_S + 30
_HOST_OFFLINE = "Host is offline. Waiting for it to reconnect…"
#: Long enough for the page to see the host return; well under the runner grace.
_HOST_RETURN_S = 30.0


def _visible_count(locator: object) -> int:
    return locator.filter(visible=True).count()  # type: ignore[attr-defined]


@pytest.mark.timeout(900)
def test_s9_host_restart_after_offline(
    lab_factory: Callable[..., Lab], browser_page: object
) -> None:
    page = browser_page
    lab = lab_factory()
    driver = SessionDriver.create(lab, "claude")
    report = ScenarioReport(
        "S9 host restart after offline", {"harness": "claude", "outage_s": _OUTAGE_S}
    )
    shots = report_dir() / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    with contract.observe(lab, driver, report):
        driver.round_trip()
        # The tool must still be running when the user quits the daemon.
        driver.start_tool_turn(_OUTAGE_S * 2)
        page.goto(f"{lab.proxies.client.url}/c/{driver.session_id}")  # type: ignore[attr-defined]
        show_view(page, "chat")

        working = page.get_by_test_id("working-indicator")  # type: ignore[attr-defined]
        started = time.time()
        faults = contract.host_offline(lab)
        try:
            time.sleep(contract.GRACE_S)
            said_offline = contract.eventually(
                lambda: True if _visible_count(working.filter(has_text=_HOST_OFFLINE)) else None,
                timeout=max(1.0, started + _OUTAGE_S - time.time()),
            )
            page.screenshot(path=str(shots / "S9-chat-past-grace.png"))  # type: ignore[attr-defined]
            report.check(
                "ui_says_host_offline_past_grace",
                bool(said_offline),
                "" if said_offline else "the working row never says the host is offline",
            )
            time.sleep(max(0.0, started + _OUTAGE_S - time.time()))
            lab.stop_host()
        finally:
            for fault in faults:
                fault.clear()
        lab.start_host()

        show_view(page, "terminal")
        resume = page.get_by_role("button", name="Resume")  # type: ignore[attr-defined]
        starting_up = page.get_by_test_id("terminal-starting-up")  # type: ignore[attr-defined]
        live_terminal = page.locator(".xterm")  # type: ignore[attr-defined]
        settled = contract.eventually(
            lambda: (
                True
                if lab.snapshot(driver.session_id).get("status") != "running"
                and not _visible_count(starting_up)
                and (_visible_count(resume) or _visible_count(live_terminal))
                else None
            ),
            timeout=_HOST_RETURN_S,
        )
        page.screenshot(path=str(shots / "S9-terminal-after-host-returns.png"))  # type: ignore[attr-defined]
        report.check(
            "terminal_leaves_starting_up_after_host_returns",
            bool(settled),
            ""
            if settled
            else f"{_HOST_RETURN_S:.0f}s after the host returned without its runner: "
            f"status={lab.snapshot(driver.session_id).get('status')}, "
            f'"Starting up…" shown={bool(_visible_count(starting_up))}',
        )

        held_turn_ended = contract.eventually(
            lambda: True if lab.snapshot(driver.session_id).get("status") != "running" else None,
            timeout=contract.RECOVERY_S + 60,
        )
        next_turn = contract.eventually_round_trip(driver) if held_turn_ended else None
        detail = "a new message after the host returned got no reply"
        if not held_turn_ended:
            detail = "the held turn was still running, so no new message was sent"
        report.check("next_turn_round_trips", next_turn is not None, "" if next_turn else detail)
    report.require()
