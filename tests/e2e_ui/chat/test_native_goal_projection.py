"""Native event -> persisted marker -> rendered sidebar and chat frame.

Uses only synthetic reports; does not claim to exercise a native TUI.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect


def test_native_goal_projection_refresh_pause_and_clear(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"title": "Review release checklist"}
    ).raise_for_status()

    def report(state: str | None) -> None:
        response = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={"type": "external_goal_state", "data": {"state": state}},
        )
        response.raise_for_status()

    screenshots = os.environ.get("OMNIGENT_GOAL_SCREENSHOTS")

    def capture(name: str) -> None:
        if screenshots:
            target = Path(screenshots)
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / f"{name}.png"), full_page=True)

    page.set_viewport_size({"width": 1440, "height": 960})
    report("active")
    page.goto(f"{base_url}/c/{session_id}")
    frame = page.get_by_test_id("session-goal-frame")
    expect(frame).to_have_attribute("data-goal-state", "active")
    opener = page.get_by_role("button", name="Open sidebar", exact=True)
    if opener.is_visible():
        opener.click()
    expect(page.get_by_role("img", name="Goal active", exact=True)).to_be_visible()
    assert frame.evaluate("e => getComputedStyle(e).pointerEvents") == "none"
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.fill("Check the release notes before publishing.")
    expect(composer).to_contain_text("Check the release notes")
    capture("goal-active-desktop")
    page.reload()
    expect(frame).to_have_attribute("data-goal-state", "active")
    report("paused")
    expect(frame).to_have_attribute("data-goal-state", "paused", timeout=15000)
    expect(page.get_by_role("img", name="Goal paused", exact=True)).to_be_visible()
    capture("goal-paused-desktop")
    page.emulate_media(color_scheme="dark")
    page.reload()
    expect(frame).to_have_attribute("data-goal-state", "paused")
    capture("goal-paused-dark")
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    expect(frame).to_have_attribute("data-goal-state", "paused")
    capture("goal-paused-mobile")
    report(None)
    expect(frame).to_have_count(0, timeout=15000)
    page.reload()
    expect(frame).to_have_count(0)
    expect(page.get_by_role("img", name="Goal paused", exact=True)).to_have_count(0)
