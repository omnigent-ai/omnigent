"""The phone glass composer keeps a typed draft readable over white transcript content.

In dark mode the draft is near-white, so the composer's backing must stay dark even when a
white screenshot from the assistant's reply scrolls behind it.
"""

from __future__ import annotations

import base64
import io
import json
import re

from playwright.sync_api import Locator, Page, Route, expect

from tests._helpers.polling import poll_until
from tests.browser_ui.chat._geometry_helpers import PHONE, open_live, seed_items, tag_scroller
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    list_payload,
    message_item,
)

_DRAFT = "Is the button label centered in this story?"
_MIN_TEXT_CONTRAST = 4.5


def _white_story_screenshot() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 480), "white")
    ImageDraw.Draw(image).rounded_rectangle(
        (300, 40, 420, 80), radius=8, outline="#d4d4d8", fill="#fafafa"
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _serve_workspace_screenshots(chat: ChatSessionContract) -> None:
    environment = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(environment, {"metadata": {"root": "/workspace"}})
    chat.contract.json(f"{environment}/changes", list_payload([]))
    chat.contract.json(
        f"{environment}/filesystem", {"available": True, "data": [], "has_more": False}
    )
    screenshot = _white_story_screenshot()

    def fulfill(route: Route) -> None:
        path = route.request.url.split("/filesystem/", 1)[1].split("?", 1)[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            headers={"X-Omnigent-File-Scope": "workspace"},
            body=json.dumps(
                {
                    "object": "session.environment.filesystem.file_content",
                    "path": path,
                    "content_type": "image/png",
                    "encoding": "base64",
                    "content": base64.b64encode(screenshot).decode(),
                    "bytes": len(screenshot),
                }
            ),
        )

    chat.contract.route(re.compile(re.escape(f"{environment}/filesystem/stories/")), fulfill)


def _seed_story_review(chat: ChatSessionContract) -> None:
    _serve_workspace_screenshots(chat)
    chronological = []
    for index in range(3):
        response_id = f"story-response-{index}"
        chronological += [
            message_item(
                f"story-user-{index}", "user", f"Review story {index}.", response_id=response_id
            ),
            message_item(
                f"story-assistant-{index}",
                "assistant",
                f"Story {index} renders its label on a white canvas:\n\n"
                f"![Story {index}](stories/story-{index}.png)",
                response_id=response_id,
            ),
        ]
    seed_items(chat, list(reversed(chronological)))


def _scroll_last_image_behind(page: Page, composer: Locator) -> None:
    image = page.get_by_role("img", name="Story 2")
    expect(image).to_be_visible()
    page.evaluate(
        """() => {
          const scroller = document.querySelector('[data-pw-scroller]');
          const image = [...document.querySelectorAll('img[alt="Story 2"]')].at(-1);
          const input = document.querySelector('textarea[aria-label="Message the agent"]');
          const imageBox = image.getBoundingClientRect();
          const inputBox = input.getBoundingClientRect();
          scroller.scrollTop += imageBox.top + imageBox.height / 2
            - (inputBox.top + inputBox.height / 2);
          scroller.dispatchEvent(new Event('scroll'));
        }"""
    )

    def image_covers_input() -> bool:
        image_box, input_box = image.bounding_box(), composer.bounding_box()
        if not (image_box and input_box):
            return False
        return (
            image_box["x"] <= input_box["x"]
            and input_box["x"] + input_box["width"] <= image_box["x"] + image_box["width"]
            and image_box["y"] < input_box["y"]
            and input_box["y"] + input_box["height"] < image_box["y"] + image_box["height"]
        )

    poll_until(image_covers_input, timeout_s=5, interval_s=0.05)


def _relative_luminance(rgb: tuple[int, ...]) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb[:3])
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: float, second: float) -> float:
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _draft_contrast(composer: Locator) -> dict:
    """Contrast between the draft's text color and the composited backing behind it."""
    from PIL import Image

    text_rgb = composer.evaluate(
        "el => getComputedStyle(el).color.match(/[\\d.]+/g).slice(0, 3).map(Number)"
    )
    pixels = Image.open(io.BytesIO(composer.screenshot(animations="disabled"))).convert("RGB")
    luminances = sorted(_relative_luminance(pixel) for pixel in pixels.get_flattened_data())
    # Glyphs cover a minority of the input box, so the median pixel is the backing.
    backing = luminances[len(luminances) // 2]
    text = _relative_luminance(tuple(round(channel) for channel in text_rgb))
    return {"text_rgb": text_rgb, "backing_luminance": backing, "ratio": _contrast(text, backing)}


def test_phone_glass_composer_draft_stays_readable_over_white_content(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    _seed_story_review(chat)
    page.emulate_media(color_scheme="dark")
    open_live(page, chat, PHONE)
    assert page.evaluate("() => document.documentElement.classList.contains('dark')")
    expect(page.locator("[data-composer-card] .omni-glass-surface")).to_be_visible()
    tag_scroller(page)

    composer = page.get_by_label("Message the agent")
    composer.click()
    composer.press_sequentially(_DRAFT, delay=20)
    expect(composer).to_have_value(_DRAFT)
    _scroll_last_image_behind(page, composer)

    measured = _draft_contrast(composer)
    assert measured["ratio"] >= _MIN_TEXT_CONTRAST, measured
