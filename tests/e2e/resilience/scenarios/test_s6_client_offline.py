"""S6: the user's browser loses the network while the host keeps working.

Only the client link breaks. ``blackhole`` leaves the page's connections
half-open (a dropped Wi-Fi), ``offline`` resets them and refuses new ones.
The turn's tool finishes on the host meanwhile. Contract: when the network
returns, the open page catches up without a reload. It shows the reply once
and no error, a pending approval can be answered from the page, and the next
message works. Needs Playwright's Chromium and a built web UI; skipped otherwise.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.e2e.resilience.lab.driver import ClaudeDriver
from tests.e2e.resilience.lab.lab import Lab
from tests.e2e.resilience.lab.observe import SessionWatcher
from tests.e2e.resilience.lab.proxy import Fault
from tests.e2e.resilience.lab.report import ScenarioReport, report_dir
from tests.e2e.resilience.scenarios import _contract as contract

sync_api = pytest.importorskip("playwright.sync_api")

_WEB_UI = Path(__file__).resolve().parents[4] / "omnigent" / "server" / "static" / "web-ui"
_COMPOSER = "Send a message…"
_UI_TIMEOUT_MS = 30_000
_CONNECTION_HINT = re.compile(r"reconnect|offline|connection|disconnected", re.IGNORECASE)
_PHASES = [contract.TOOL_ENDS_DURING_OUTAGE, contract.APPROVAL_PENDING]


@pytest.fixture
def browser_page() -> Iterator[object]:
    if not (_WEB_UI / "index.html").is_file():
        pytest.skip("web UI is not built; run `pnpm --filter web run build`")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            yield page
        finally:
            browser.close()


def _offline(lab: Lab, mode: str) -> list[Fault]:
    client = lab.proxies.client
    if mode == "blackhole":
        return [client.blackhole()]
    fault = client.refuse()
    client.reset()
    return [fault]


@pytest.mark.timeout(900)
@pytest.mark.parametrize("mode", ["blackhole", "offline"])
@pytest.mark.parametrize(
    ("phase", "outage_s"), contract.cases(_PHASES, contract.outages([20], [120]))
)
def test_s6_client_offline(
    lab_factory: Callable[..., Lab], browser_page: object, mode: str, phase: str, outage_s: int
) -> None:
    page = browser_page
    expect = sync_api.expect
    lab = lab_factory()
    session_id = lab.create_claude_session()
    driver = ClaudeDriver(lab, session_id)
    report = ScenarioReport(
        "S6 client offline", {"mode": mode, "phase": phase, "outage_s": outage_s}
    )
    shots = report_dir() / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    stem = f"S6-{mode}-{phase}-{outage_s}s"
    with SessionWatcher(lab.server_url, session_id) as watcher:
        warmup = driver.round_trip()
        page.goto(f"{lab.proxies.client.url}/c/{session_id}")  # type: ignore[attr-defined]
        _chat_view(page)
        expect(page.get_by_text(warmup.reply)).to_be_visible(timeout=_UI_TIMEOUT_MS)  # type: ignore[attr-defined]
        if phase == contract.APPROVAL_PENDING:
            turn, _ = driver.start_approval_turn()
            expect(page.get_by_test_id("approval-card")).to_be_visible(timeout=_UI_TIMEOUT_MS)  # type: ignore[attr-defined]
        else:
            turn = driver.start_tool_turn(max(1.0, outage_s - 8))

        started = time.time()
        faults = _offline(lab, mode)
        try:
            time.sleep(outage_s / 2)
            hint = page.get_by_text(_CONNECTION_HINT).count()  # type: ignore[attr-defined]
            page.screenshot(path=str(shots / f"{stem}-during.png"))  # type: ignore[attr-defined]
            report.check(
                "ui_signals_lost_connection",
                hint > 0,
                "" if hint else "nothing on screen says the page is offline or reconnecting",
                known_gap="R5",
            )
            time.sleep(max(0.0, started + outage_s - time.time()))
        finally:
            for fault in faults:
                fault.clear()
        ended = time.time()

        if phase == contract.APPROVAL_PENDING:
            card = page.get_by_test_id("approval-card")  # type: ignore[attr-defined]
            visible = _visible(lambda: expect(card).to_be_visible(timeout=_UI_TIMEOUT_MS))
            report.check("ui_approval_card_still_answerable", visible)
            if visible:
                card.get_by_role("button", name="Approve", exact=True).click()
        caught_up = _visible(
            lambda: expect(page.get_by_text(turn.reply)).to_be_visible(  # type: ignore[attr-defined]
                timeout=int(contract.RECOVERY_S * 1000)
            )
        )
        page.screenshot(path=str(shots / f"{stem}-after.png"))  # type: ignore[attr-defined]
        report.check("ui_catches_up_without_reload", caught_up)
        copies = page.get_by_text(turn.reply).count()  # type: ignore[attr-defined]
        report.check("ui_shows_reply_once", copies == 1, f"{copies} copies")
        errors = page.get_by_test_id("error-pill").count()  # type: ignore[attr-defined]
        report.check("ui_shows_no_error", errors == 0, f"{errors} error pill(s)")
        contract.check_turn_completes(report, driver, turn)

        follow = driver.text_turn()
        _send(page, f"Run the task for {follow.marker}")
        replied = _visible(
            lambda: expect(page.get_by_text(follow.reply)).to_be_visible(  # type: ignore[attr-defined]
                timeout=int(contract.RECOVERY_S * 1000)
            )
        )
        report.check("ui_next_message_round_trips", replied)
        contract.check_settled_idle(report, lab, session_id)
        contract.check_no_failure(report, watcher, started, ended + contract.SETTLE_S)
    report.attach(watcher, lab.root)
    report.require()


def _chat_view(page: object) -> None:
    expect = sync_api.expect
    toggle = page.get_by_test_id("view-mode-toggle")  # type: ignore[attr-defined]
    expect(toggle).to_be_visible(timeout=_UI_TIMEOUT_MS)
    # The host's first-connect import review can open over the session.
    dialog = page.get_by_role("dialog")  # type: ignore[attr-defined]
    if dialog.count():
        dialog.get_by_role("button", name="Close").first.click()
        expect(dialog).to_have_count(0, timeout=_UI_TIMEOUT_MS)
    chat = page.get_by_test_id("view-mode-chat")  # type: ignore[attr-defined]
    if chat.get_attribute("aria-pressed") != "true":
        chat.click()


def _send(page: object, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)  # type: ignore[attr-defined]
    sync_api.expect(composer).to_be_visible(timeout=_UI_TIMEOUT_MS)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()  # type: ignore[attr-defined]


def _visible(assertion: Callable[[], None]) -> bool:
    try:
        assertion()
    except AssertionError:
        return False
    return True
