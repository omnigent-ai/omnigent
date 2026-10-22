"""E2E: the local Composer submit shortcut persists and controls submission."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, expect

_STORAGE_KEY = "omnigent:composer-submit-with-mod-enter"
_COMPOSER_LABEL = "Message the agent"


def _record_message_posts(page: Page, session_id: str) -> list[str]:
    posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if not isinstance(body, dict) or body.get("type") != "message":
            return
        for block in body.get("data", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "input_text":
                posts.append(str(block.get("text", "")))

    page.on("request", record)
    return posts


def _wait_for_posts(page: Page, posts: list[str], count: int) -> None:
    deadline = time.monotonic() + 30
    while len(posts) < count and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert len(posts) == count


def _screenshot(page: Page) -> None:
    shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if shot_dir:
        Path(shot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(shot_dir) / "composer-submit-shortcut.png"))


def test_submit_with_mod_enter_persists_and_is_the_only_send_gesture(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    posts = _record_message_posts(page, session_id)

    page.goto(f"{base_url}/settings/general")
    expect(page.get_by_role("heading", name="Composer", exact=True)).to_be_visible(timeout=30_000)
    toggle = page.get_by_test_id("composer-submit-with-mod-enter-toggle")
    expect(toggle).to_have_attribute("aria-checked", "false")
    is_mac = page.evaluate(
        "() => /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || '')"
    )
    mod_key = "⌘" if is_mac else "Ctrl"
    expect(
        page.get_by_text(f"Submit with {mod_key} + Enter on desktop", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("radio")).to_have_count(0)
    assert page.evaluate(f"localStorage.getItem('{_STORAGE_KEY}')") is None

    started = time.perf_counter()
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    assert page.evaluate(f"localStorage.getItem('{_STORAGE_KEY}')") == "true"
    print(f"composer_shortcut_toggle_ms={(time.perf_counter() - started) * 1000:.2f}")

    page.reload()
    toggle = page.get_by_test_id("composer-submit-with-mod-enter-toggle")
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=30_000)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("first line")
    composer.press("Enter")
    composer.type("second line")
    expect(composer).to_have_value("first line\nsecond line")
    page.wait_for_timeout(300)
    assert posts == []

    composer.press("Meta+Enter" if is_mac else "Control+Enter")
    _wait_for_posts(page, posts, 1)
    assert posts == ["first line\nsecond line"]

    composer.fill("plain Enter stays multiline")
    composer.press("Enter")
    expect(composer).to_have_value("plain Enter stays multiline\n")
    page.wait_for_timeout(300)
    assert posts == ["first line\nsecond line"]

    page.goto(f"{base_url}/settings/general")
    expect(page.get_by_test_id("composer-submit-with-mod-enter-toggle")).to_have_attribute(
        "aria-checked", "true"
    )
    _screenshot(page)


def test_alt_enter_inserts_newline_in_both_composers(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Alt/Option+Enter adds a line break like Shift+Enter; plain Enter still sends.

    Chromium inserts nothing for Alt+Enter in a textarea, so this needs a real
    browser: a component test can't tell a native insert from the composer's.
    """
    base_url, session_id = seeded_session
    posts = _record_message_posts(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("")
    composer.press_sequentially("first line")
    composer.press("Alt+Enter")
    composer.press_sequentially("second line")
    composer.press("Shift+Enter")
    composer.press_sequentially("third line")
    expect(composer).to_have_value("first line\nsecond line\nthird line")
    page.wait_for_timeout(300)
    assert posts == []

    composer.press("Enter")
    _wait_for_posts(page, posts, 1)
    assert posts == ["first line\nsecond line\nthird line"]

    page.goto(base_url)
    landing = page.get_by_test_id("new-chat-landing-input")
    expect(landing).to_be_visible(timeout=30_000)
    landing.press_sequentially("plan")
    landing.press("Alt+Enter")
    landing.press_sequentially("then build")
    expect(landing).to_have_value("plan\nthen build")
    expect(page).to_have_url(f"{base_url}/")
