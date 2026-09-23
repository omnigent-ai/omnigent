"""Queued attachments stay visible in a compact row, with or without message text."""

from __future__ import annotations

import base64
import json

import pytest
from playwright.sync_api import Page, Route, expect

_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
_FILENAMES = ["Screenshot 2026-09-18 at 11.49.24 AM.png", "after.png", "notes.txt"]


@pytest.mark.parametrize("width", [1280, 390, 320], ids=["desktop", "phone", "narrow-phone"])
@pytest.mark.parametrize("screenshot_name", [_FILENAMES[0], ""], ids=["named", "unnamed"])
@pytest.mark.parametrize(
    "text",
    ["", "Hey", "Compare these screenshots. " * 30],
    ids=["attachment-only", "short", "long"],
)
def test_queued_attachments_share_a_compact_row(
    page: Page,
    seeded_session: tuple[str, str],
    width: int,
    screenshot_name: str,
    text: str,
) -> None:
    """Queue real Files without uploading; keep their chip visible next to truncated text."""
    base_url, session_id = seeded_session
    expected_names = [screenshot_name or "image.png", *_FILENAMES[1:]]
    page.set_viewport_size({"width": width, "height": 844})
    event_posts: list[str] = []
    uploads: list[str] = []

    def ack_event(route: Route) -> None:
        event_posts.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_attachment_chip"}),
        )

    def reject_upload(route: Route) -> None:
        uploads.append(route.request.url)
        route.abort()

    # Acknowledging without an idle event keeps the first turn busy locally.
    page.route("**/v1/sessions/*/events", ack_event)
    page.route("**/v1/sessions/*/resources/files", reject_upload)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Hold this turn open for the attachment queue test.")
    send = page.get_by_role("button", name="Send", exact=True)
    with page.expect_response(lambda response: response.url.endswith(f"/{session_id}/events")):
        send.click()

    page.locator('form.chat-composer-form input[type="file"]').set_input_files(
        [
            {"name": screenshot_name, "mimeType": "image/png", "buffer": _IMAGE},
            {"name": _FILENAMES[1], "mimeType": "image/png", "buffer": _IMAGE},
            {"name": _FILENAMES[2], "mimeType": "text/plain", "buffer": b"Notes"},
        ]
    )
    composer.fill(text)
    send.click()

    strip = page.get_by_test_id("composer-queued-strip")
    chip = strip.get_by_test_id("queued-message-attachments")
    expect(chip).to_be_visible()
    expect(chip).to_have_attribute("title", "\n".join(expected_names))
    expect(chip.get_by_text(expected_names[0], exact=True)).to_be_visible()
    expect(chip.get_by_text("+2", exact=True)).to_be_visible()
    assert len(event_posts) == 1, "The attachment message was sent instead of queued"
    assert not uploads, "Rendering queued attachments must not need an upload"

    layout = chip.evaluate(
        """chip => {
            const text = chip.previousElementSibling;
            const box = chip.getBoundingClientRect();
            const textBox = text?.getBoundingClientRect();
            const row = chip.parentElement.parentElement;
            const action = row.querySelector('[aria-label="Send queued message now"]');
            return {
                nameWidth: chip.querySelector('.truncate').clientWidth,
                textWidth: textBox?.width,
                sameLine: !textBox || Math.abs(
                    box.y + box.height / 2 - textBox.y - textBox.height / 2
                ) < 1,
                noOverlap: box.right <= action.getBoundingClientRect().left,
                rowHeight: row.getBoundingClientRect().height,
            };
        }"""
    )
    if width >= 768:
        assert layout["sameLine"], layout
    assert layout["noOverlap"], layout
    assert layout["nameWidth"] >= 24, layout
    # Desktop uses 24px compact controls plus 2px vertical padding on each
    # side; mobile keeps the same padding around its 44px touch targets.
    assert layout["rowHeight"] <= (48 if width < 768 else 28), layout
    if text:
        assert layout["textWidth"] >= 16, layout
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    # Editing restores the original text and all Files, not the display summary.
    strip.get_by_role("button", name="Edit queued message", exact=True).click()
    expect(strip).to_have_count(0)
    expect(composer).to_have_value(text.strip())
    for name in expected_names:
        expect(page.get_by_role("button", name=f"Remove {name}", exact=True)).to_be_visible()
    send.click()
    expect(chip).to_be_visible()
    expect(chip).to_have_attribute("title", "\n".join(expected_names))
    strip.get_by_role("button", name="Remove queued message", exact=True).click()
    expect(strip).to_have_count(0)
    assert len(event_posts) == 1
    assert not uploads
