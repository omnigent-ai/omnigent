"""S9: the host goes offline mid-turn, then the user restarts it.

A tool is running when the host's links are refused, as a laptop losing its
network. Past the liveness grace the user quits the host with Ctrl-C and
starts it again. Contract: while the host is offline the chat says so instead
of a working word; once the host is back without the turn's runner, the
terminal leaves "Starting up…" promptly and a "continue" typed without Stop is
answered. Needs Playwright's Chromium and a built web UI; skipped otherwise.
Set ``OMNIGENT_E2E_RECORD_DIR`` to record the page.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.conftest import BROWSER_VIEWPORT
from tests.e2e.resilience.lab.driver import SessionDriver
from tests.e2e.resilience.lab.lab import Lab
from tests.e2e.resilience.lab.recorder import show_view
from tests.e2e.resilience.lab.report import ScenarioReport, report_dir
from tests.e2e.resilience.scenarios import _contract as contract

sync_api = pytest.importorskip("playwright.sync_api")

_OUTAGE_S = contract.GRACE_S + 30
#: How soon after the host is back the terminal must stop "Starting up…".
_HOST_RETURN_S = 30.0
#: How long to keep watching the terminal so the report says when it moved on.
_TERMINAL_WATCH_S = 180.0
_REPLY_S = 120.0
_UI_TIMEOUT_MS = 60_000
_OFFLINE_HINT = re.compile(r"offline|interrupted|disconnected|reconnect", re.IGNORECASE)


@pytest.mark.timeout(900)
def test_s9_host_restart_after_offline(
    lab_factory: Callable[..., Lab], chromium_browser: object
) -> None:
    lab = lab_factory()
    driver = SessionDriver.create(lab, "claude")
    session_id = driver.session_id
    report = ScenarioReport("S9 host restart after offline", {"harness": "claude"})
    shots = report_dir() / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    with contract.observe(lab, driver, report):
        driver.round_trip()
        driver.start_tool_turn(_OUTAGE_S * 4)
        context = chromium_browser.new_context(  # type: ignore[attr-defined]
            viewport=BROWSER_VIEWPORT,
            record_video_dir=record_dir,
            record_video_size=BROWSER_VIEWPORT if record_dir else None,
        )
        try:
            page = context.new_page()
            lab.events.emit("user", "open_page", action="opens the session in the browser")
            page.goto(f"{lab.proxies.client.url}/c/{session_id}")
            show_view(page, "chat")
            working = page.get_by_test_id("working-indicator")
            sync_api.expect(working).to_be_visible(timeout=_UI_TIMEOUT_MS)

            started = time.time()
            faults = contract.host_offline(lab)
            try:
                time.sleep(contract.GRACE_S + 5)
                label = working.inner_text() if working.is_visible() else ""
                hints = page.get_by_text(_OFFLINE_HINT).count()
                interrupt = page.get_by_role("button", name=re.compile("Interrupt|Stop")).count()
                page.screenshot(path=str(shots / "S9-offline-chat.png"))
                report.check(
                    "chat_says_host_offline_not_working",
                    bool(_OFFLINE_HINT.search(label)) if label else hints > 0,
                    f"working row {label!r}, {interrupt} stop control(s), "
                    f"{hints} offline hint(s), "
                    f"status={lab.snapshot(session_id).get('status')}",
                )
                time.sleep(max(0.0, started + _OUTAGE_S - time.time()))
                lab.events.emit("user", "quit_host", action="presses Ctrl-C in the host terminal")
                lab.stop_host()
                lab.events.emit(
                    "lab", "runners_after_quit", pids=[p.pid for p in lab.runner_processes()]
                )
            finally:
                for fault in faults:
                    fault.clear()
            lab.events.emit("user", "start_host", action="starts the host again")
            lab.start_host()
            host_back = time.time()

            show_view(page, "terminal")
            surface = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')

            def moved_on() -> float | None:
                starting = surface.get_by_test_id("terminal-starting-up").is_visible()
                status = lab.snapshot(session_id).get("status")
                lab.events.emit(
                    "observer",
                    "terminal_sample",
                    status=status,
                    starting_up=starting,
                    text=surface.inner_text()[:200],
                )
                return time.time() - host_back if not starting and status != "running" else None

            left_at = contract.eventually(moved_on, timeout=_TERMINAL_WATCH_S)
            page.screenshot(path=str(shots / "S9-terminal-after-host-back.png"))
            report.check(
                "terminal_moves_on_after_host_returns",
                left_at is not None and float(left_at) <= _HOST_RETURN_S,
                f"moved on after {left_at:.0f}s"
                if isinstance(left_at, float)
                else f"still waiting after {_TERMINAL_WATCH_S:.0f}s: "
                f"{surface.inner_text()[:120]!r}",
            )
            lab.events.emit("lab", "terminal_moved_on", after_s=left_at)

            show_view(page, "chat")
            follow = driver.text_turn()
            lab.events.emit("user", "send", action=f"types continue ({follow.marker})")
            composer = page.get_by_label("Message the agent")
            composer.fill(f"continue {follow.marker}")
            composer.press("Enter")
            replied = contract.eventually(
                lambda: True if page.get_by_text(follow.reply).count() else None,
                timeout=_REPLY_S,
            )
            page.screenshot(path=str(shots / "S9-after-continue.png"))
            report.check(
                "continue_without_stop_is_answered",
                bool(replied),
                "" if replied else f"no reply; status={lab.snapshot(session_id).get('status')}",
            )
        finally:
            context.close()
    report.require()
