"""Streaming completion preserves the reader's position in the real browser."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    message_item,
    transcript_items,
)

_RESPONSE_ID = "scroll-completion-response"
_SCROLLER = '[role="log"] > div'


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("scroll_away", [False, True], ids=["at-bottom", "reading-above"])
def test_streaming_completion_preserves_reading_position(
    page: Page,
    chat_session_contract: ChatSessionContract,
    width: int,
    scroll_away: bool,
) -> None:
    chat = chat_session_contract
    chat.set_items(
        [
            message_item("stream-user", "user", "Tell me a long story.", response_id=_RESPONSE_ID),
            *transcript_items(20),
        ]
    )
    page.set_viewport_size({"width": width, "height": 480})
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_text("Tell me a long story.", exact=True)).to_be_visible()
    scroller = page.locator(_SCROLLER).first
    scroller.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(300)

    chat.emit_busy(_RESPONSE_ID)
    chat.emit({"event": "response.created", "data": {"id": _RESPONSE_ID, "status": "in_progress"}})
    words = [f"streamedword{index:03d}" for index in range(160)]
    reading_top = None
    for start in range(0, len(words), 8):
        chunk = " ".join(words[start : start + 8]) + " "
        chat.emit({"event": "response.output_text.delta", "data": {"delta": chunk}})
        page.wait_for_function(
            "word => document.body.textContent.includes(word)", arg=words[start + 7]
        )
        page.wait_for_timeout(50)
        if scroll_away and start == 64:
            scroller.hover()
            page.mouse.wheel(0, -150)
            page.wait_for_timeout(250)
            reading_top = scroller.evaluate("el => el.scrollTop")
            assert (
                scroller.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop") > 100
            )

    page.wait_for_timeout(250)
    if scroll_away:
        assert scroller.evaluate("el => el.scrollTop") == pytest.approx(reading_top, abs=2)
    else:
        assert scroller.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop") <= 2

    reply = " ".join(words) + " "
    saved = message_item("saved-stream-answer", "assistant", reply, response_id=_RESPONSE_ID)
    chat.emit({"event": "response.output_item.done", "data": {"item": saved}})
    chat.emit(
        {
            "event": "response.completed",
            "data": {"id": _RESPONSE_ID, "status": "completed", "output": [saved]},
        }
    )
    chat.emit_idle(_RESPONSE_ID)
    page.wait_for_timeout(1000)

    if scroll_away:
        assert scroller.evaluate("el => el.scrollTop") == pytest.approx(reading_top, abs=2)
        assert scroller.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop") > 100
    else:
        assert scroller.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop") <= 2
