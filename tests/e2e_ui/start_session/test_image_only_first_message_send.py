"""E2E: an image-only draft (a screenshot, no text) must be sendable.

The reported journey: the user attaches a screenshot without typing any
message and hits Send. Two composers own that journey:

- the new-chat landing composer (``shell/NewChatDialog.tsx``): its
  attachment affordances (paperclip / paste / drop) accept the image and
  render its chip, but ``canSubmit`` requires non-empty *text*, so the
  Send button stays disabled ("Enter a message to get started") and the
  image-only first message can never be sent.
- the in-session composer (``pages/ChatPage.tsx``): ``hasDraft`` counts
  attached files, so an image-only message sends and runs a turn.

The landing test's route stubbing mirrors ``test_start_session.py`` (the
headless harness registers no host, so hosts/agents/create are faked),
while the attachment upload and the navigated-to session stay real. The
``/events`` stub is re-registered here to *capture* the auto-dispatched
first message so the test can assert the image-only content actually
left the composer.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Route, async_playwright
from playwright.async_api import expect as expect_async
from playwright.sync_api import Page, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

_COMPOSER = "Send a message…"

# 240x180 PNG (same fixture image as test_image_lightbox_dismiss) — stands in
# for the pasted screenshot from the report.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDI2hwdBgaDA0GBpDg6HB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)

_SCREENSHOT_NAME = "screenshot.png"


def _write_png(tmp_path: Path) -> Path:
    sample = tmp_path / _SCREENSHOT_NAME
    sample.write_bytes(base64.b64decode(_PNG_B64))
    return sample


def test_landing_image_only_draft_starts_the_session(
    seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """Attaching a screenshot with no text must enable Send and dispatch it."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_landing_image_only(base_url, session_id, _write_png(tmp_path)))


async def _drive_landing_image_only(base_url: str, session_id: str, png: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Hide agents left by other tests so the stubbed Claude agent
            # remains the auto-selected harness.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Registered after _register_common_routes so this handler wins
            # for /events: capture the auto-dispatched first message instead
            # of merely swallowing it.
            event_bodies: list[dict[str, Any]] = []

            async def capture_events(route: Route) -> None:
                if route.request.method == "POST":
                    body = route.request.post_data_json
                    if body is not None:
                        event_bodies.append(body)
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            await page.route("**/v1/sessions/*/events", capture_events)

            # Seed a recent working directory for the stubbed host so the
            # working-directory chip auto-fills and only the draft content
            # gates Send. Set before the SPA boots.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-file-input").set_input_files(str(png))
            await expect_async(
                page.get_by_role("button", name=f"Remove {_SCREENSHOT_NAME}")
            ).to_be_visible(timeout=10_000)

            submit = page.get_by_test_id("new-chat-landing-submit")
            await expect_async(submit).to_be_enabled(timeout=10_000)
            await submit.click()

            await _wait_until(lambda: len(create_bodies) == 1)

            # The first message auto-dispatches after navigation and must be
            # exactly the image the user attached — no fabricated text.
            def _image_message_posted() -> bool:
                for body in event_bodies:
                    if body.get("type") != "message":
                        continue
                    content = body.get("data", {}).get("content", [])
                    types = [block.get("type") for block in content]
                    if "input_image" in types:
                        assert "input_text" not in types, body
                        return True
                return False

            await _wait_until(_image_message_posted, timeout_s=30.0)
        finally:
            # Explicit context close finalizes any requested video recording.
            await page.context.close()
            await browser.close()


def test_session_composer_image_only_draft_sends(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """An image-only message sends from the in-session composer and runs a turn."""
    base_url, session_id = seeded_session
    png = _write_png(tmp_path)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(png))
    expect(page.get_by_role("button", name=f"Remove {_SCREENSHOT_NAME}")).to_be_visible(
        timeout=10_000
    )

    send = page.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_enabled(timeout=10_000)
    send.click()

    user_bubble = page.locator('[data-testid="message-bubble"][data-role="user"]')
    expect(user_bubble.locator("img").first).to_be_visible(timeout=30_000)
    # Reply text is not pinned: the title-generation LLM call can consume
    # scripted mock-queue entries before the turn's own call.
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    ).to_be_visible(timeout=60_000)
    expect(page.locator('[data-testid="working-indicator"]')).to_have_count(0, timeout=60_000)
