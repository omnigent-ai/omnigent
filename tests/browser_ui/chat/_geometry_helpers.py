"""Geometry probes shared by the browser-only composer contract."""

from __future__ import annotations

import re
import time
from itertools import pairwise

import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload, message_item

TOLERANCE = 1.0
PHONE = {"width": 390, "height": 664}
TABLET = {"width": 768, "height": 1024}
DESKTOP = {"width": 1280, "height": 852}
WIDE = {"width": 1800, "height": 900}


def box(locator: Locator) -> FloatRect:
    value = locator.bounding_box()
    assert value is not None, f"{locator} has no bounding box"
    return value


def right_inset(container: FloatRect, child: FloatRect) -> float:
    return container["x"] + container["width"] - (child["x"] + child["width"])


def assert_symmetric_insets(card: FloatRect, container: FloatRect) -> None:
    left = card["x"] - container["x"]
    right = right_inset(container, card)
    assert abs(left - right) <= TOLERANCE, (left, right, card, container)


def assert_same_vertical_center(left: FloatRect, right: FloatRect) -> None:
    left_center = left["y"] + left["height"] / 2
    right_center = right["y"] + right["height"] / 2
    assert abs(left_center - right_center) <= TOLERANCE, (left_center, right_center)


def assert_no_overlap(left: FloatRect, right: FloatRect) -> None:
    overlap_x = min(left["x"] + left["width"], right["x"] + right["width"]) - max(
        left["x"], right["x"]
    )
    overlap_y = min(left["y"] + left["height"], right["y"] + right["height"]) - max(
        left["y"], right["y"]
    )
    assert not (overlap_x > TOLERANCE and overlap_y > TOLERANCE), (overlap_x, overlap_y)


def assert_within_viewport(value: FloatRect, width: int) -> None:
    assert value["x"] >= -TOLERANCE
    assert value["x"] + value["width"] <= width + TOLERANCE


def assert_row_grid(rows: list[FloatRect]) -> None:
    first = rows[0]
    for row in rows[1:]:
        assert row["x"] == pytest.approx(first["x"], abs=TOLERANCE)
        assert row["width"] == pytest.approx(first["width"], abs=TOLERANCE)
        assert row["height"] == pytest.approx(first["height"], abs=TOLERANCE)
    for before, after in pairwise(rows):
        assert after["y"] - before["y"] == pytest.approx(before["height"], abs=TOLERANCE)


def open_live(
    page: Page,
    chat: ChatSessionContract,
    viewport: dict[str, int] = DESKTOP,
) -> None:
    page.set_viewport_size(viewport)
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("composer-action-row")).to_be_visible(timeout=15_000)


def open_landing(
    page: Page,
    chat: ChatSessionContract,
    viewport: dict[str, int] = DESKTOP,
) -> None:
    chat.contract.json(
        re.compile(r"/v1/hosts/[^/]+/filesystem(?:[/?].*)?$"),
        {"available": True, "data": [], "has_more": False},
    )
    page.set_viewport_size(viewport)
    page.goto(chat.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=15_000)


def seed_long_transcript(
    chat: ChatSessionContract,
    turns: int = 30,
    *,
    paragraphs: int | None = None,
) -> None:
    if paragraphs is None:
        chat.seed_transcript(turns)
        return
    chronological: list[dict] = []
    for index in range(turns):
        response_id = f"geometry-response-{index}"
        chronological.extend(
            [
                message_item(
                    f"geometry-user-{index}",
                    "user",
                    f"Question {index}?",
                    response_id=response_id,
                ),
                message_item(
                    f"geometry-assistant-{index}",
                    "assistant",
                    f"Paragraph {index}. "
                    + "\n\n".join(
                        f"detail line {index}.{line} with filler text"
                        for line in range(paragraphs)
                    ),
                    response_id=response_id,
                ),
            ]
        )
    seed_items(chat, list(reversed(chronological)))


def seed_items(chat: ChatSessionContract, newest_first: list[dict]) -> None:
    """Install special transcript rows without expanding the shared fixture API."""
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/items",
        list_payload(newest_first),
    )


GEOMETRY_PROBE = """() => {
    const ta = document.querySelector('textarea[aria-label="Message the agent"]');
    const form = ta.closest('form');
    const card = form.querySelector('[data-composer-card]');
    const scroller = form.parentElement.querySelector('[role="log"] > div');
    const rail = document.querySelector('.turn-rail-fade');
    const sections = [...document.querySelectorAll('[data-testid="assistant-text-section"]')];
    const composerTop = Math.round(card.parentElement.getBoundingClientRect().top);
    const transcriptBottom = Math.round(scroller.getBoundingClientRect().bottom);
    return {
        messageTops: sections.map((section) => Math.round(section.getBoundingClientRect().top)),
        lastMessageBottom: Math.round(sections.at(-1).getBoundingClientRect().bottom),
        composerHeight: Math.round(ta.getBoundingClientRect().height),
        composerTop,
        transcriptBottom,
        overlap: transcriptBottom - composerTop,
        formMarginTop: Math.round(parseFloat(getComputedStyle(form).marginTop) || 0),
        distanceFromBottom: Math.round(
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop),
        viewport: [scroller.clientHeight, scroller.scrollHeight, Math.round(scroller.scrollTop)],
        railTicks: rail ? [...rail.querySelectorAll('button')]
            .map((tick) => Math.round(tick.getBoundingClientRect().top)) : null,
    };
}"""


def settled_geometry(page: Page, timeout_s: float = 15.0) -> dict:
    previous = page.evaluate(GEOMETRY_PROBE)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(100)
        current = page.evaluate(GEOMETRY_PROBE)
        if current == previous:
            return current
        previous = current
    raise AssertionError(f"layout never settled; last reading: {previous}")


def tag_scroller(page: Page) -> None:
    overflowed = page.evaluate(
        """() => {
          const el = document.querySelector('.transcript-hide-native-scrollbar');
          if (!el) return false;
          el.dataset.pwScroller = '1';
          return el.scrollHeight > el.clientHeight + 4;
        }"""
    )
    assert overflowed, "transcript did not overflow"


def scroll_state(page: Page) -> dict:
    return page.evaluate(
        """() => {
          const el = document.querySelector('[data-pw-scroller]');
          return {
            scrollTop: Math.round(el.scrollTop),
            max: Math.round(el.scrollHeight - el.clientHeight),
          };
        }"""
    )


def park_at_bottom(page: Page) -> dict:
    page.eval_on_selector("[data-pw-scroller]", "el => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(400)
    state = scroll_state(page)
    assert state["max"] - state["scrollTop"] <= 2, state
    return state


def drag_thumb_with_mouse(page: Page, dy: float) -> None:
    thumb = page.get_by_test_id("transcript-scrollbar-thumb")
    value = box(thumb)
    x = value["x"] + value["width"] / 2
    y = value["y"] + value["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 9):
        page.mouse.move(x, y + dy * step / 8)
        page.wait_for_timeout(16)
    page.mouse.up()
    page.wait_for_timeout(300)


def drag_thumb_with_touch(page: Page, dy: float) -> None:
    session = page.context.new_cdp_session(page)
    try:
        session.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
        value = box(page.get_by_test_id("transcript-scrollbar-thumb"))
        x = round(value["x"] + value["width"] / 2)
        y = round(value["y"] + value["height"] / 2)
        point = {"x": x, "y": y, "id": 1, "radiusX": 2, "radiusY": 2, "force": 1}
        session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
        for step in range(1, 9):
            session.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchMove",
                    "touchPoints": [{**point, "y": round(y + dy * step / 8)}],
                },
            )
            page.wait_for_timeout(16)
        session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(500)
    finally:
        session.detach()
