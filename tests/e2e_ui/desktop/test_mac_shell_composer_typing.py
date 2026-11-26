"""Composer keystroke cost in a browser tab and the emulated macOS desktop shell."""

from __future__ import annotations

import json
import statistics

import pytest
from playwright.sync_api import CDPSession, Page, expect

from tests.e2e_ui.desktop._mac_shell import MAC_SHELL_INIT_SCRIPT

TEXT = "The quick brown fox jumps over the lazy dog"


def _trace_events(cdp: CDPSession, page: Page) -> list[dict]:
    completed: dict = {}
    cdp.on("Tracing.tracingComplete", lambda params: completed.update(params))
    cdp.send("Tracing.end")
    for _ in range(300):
        if "stream" in completed:
            break
        page.wait_for_timeout(100)
    assert "stream" in completed, "the trace did not complete within 30 seconds"
    chunks = []
    while True:
        chunk = cdp.send("IO.read", {"handle": completed["stream"]})
        chunks.append(chunk["data"])
        if chunk.get("eof"):
            cdp.send("IO.close", {"handle": completed["stream"]})
            return json.loads("".join(chunks))["traceEvents"]


def _restyled_elements_per_keystroke(events: list[dict]) -> list[int]:
    keydowns = sorted(
        event["ts"]
        for event in events
        if event.get("name") == "EventDispatch"
        and event.get("args", {}).get("data", {}).get("type") == "keydown"
    )
    restyles = [
        (event["ts"], event.get("args", {}).get("elementCount", 0))
        for event in events
        if event.get("name") == "UpdateLayoutTree" and event.get("ph") == "X"
    ]
    bounds = [*keydowns, float("inf")]
    return [
        sum(count for ts, count in restyles if bounds[index] <= ts < bounds[index + 1])
        for index in range(len(keydowns))
    ]


@pytest.mark.parametrize("mac_shell", [False, True], ids=["browser", "mac_shell"])
def test_composer_keystroke_restyles_only_the_composer(
    page: Page, seeded_session: tuple[str, str], mac_shell: bool
) -> None:
    base_url, session_id = seeded_session
    if mac_shell:
        page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    if mac_shell:
        expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
    composer = page.get_by_label("Message the agent")
    composer.click()
    page_elements = page.evaluate("() => document.querySelectorAll('*').length")

    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Tracing.start", {"categories": "devtools.timeline", "transferMode": "ReturnAsStream"}
    )
    page.keyboard.type(TEXT, delay=40)
    expect(composer).to_have_value(TEXT)
    # Let the last keystroke's restyle land in the trace before it ends.
    page.evaluate(
        "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
    )
    per_keystroke = _restyled_elements_per_keystroke(_trace_events(cdp, page))

    assert len(per_keystroke) == len(TEXT)
    median = statistics.median(per_keystroke)
    # Each keystroke restyles the composer itself; a zero median means nothing was measured.
    assert 0 < median < page_elements / 4, (
        f"a keystroke restyled a median of {median} elements on a page of {page_elements}"
    )
