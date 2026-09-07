"""E2E: an inline image preview must hold its space before the bytes arrive.

An attached image used to render into a box with no reserved height: the
``<img>`` carried ``max-h-64`` but no intrinsic size, so it laid out at ~0 tall
and jumped to as much as 256px once the bytes decoded. Everything below it was
displaced at that moment, and nothing in the chat surface absorbs that — the
transcript scroller runs with ``[overflow-anchor:none]`` because
``HistoryAutoLoader`` owns prepend anchoring, and
``PreserveScrollDistanceOnResize`` early-returns off iOS.

This is invisible below the browser: jsdom has no layout, so a component test
can assert the box's classes but never that the image actually occupies the
space they promise, nor that the text below it stays put.

Rather than race the network, the test compares the two states directly — the
same transcript rendered with the image bytes blocked, and with them served. A
reserved box is the same height either way, so the reply beneath it lands on
exactly the same pixel. Before the fix the blocked render collapsed and that
reply sat hundreds of pixels higher.
"""

from __future__ import annotations

import base64
import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

_IMAGE_NAME = "shot.png"
_REPLY = "Reviewing the screenshot."

# A real 240x180 PNG, inline so the fixture needs no image library. Its natural
# size is irrelevant to the assertions — what matters is that it decodes, so the
# "served" render is a genuinely loaded image rather than a second broken one.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDI2hwdBgaDA0GBpDg6HB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)

# The preview box and the reply below it, read together so a measurement can't
# straddle a layout pass. ``closest('div')`` is the box itself — the lightbox
# wraps the image in a button, so the image's own parent is that button.
_PROBE = """() => {
    const img = document.querySelector('img[alt="shot.png"]');
    const section = document.querySelector('[data-testid="assistant-text-section"]');
    return {
        boxHeight: img ? Math.round(img.closest('div').getBoundingClientRect().height) : null,
        replyTop: section ? Math.round(section.getBoundingClientRect().top) : null,
    };
}"""


def _seed_image_turn(base_url: str, session_id: str) -> None:
    """Attach an image to *session_id* and commit a turn that renders it.

    Uploads through the real file endpoint so the transcript's
    ``input_image`` block resolves to genuine stored bytes, then writes the
    turn straight to the store — the same shortcut
    :func:`tests.e2e_ui.conftest.seed_committed_turn` takes, so no agent turn
    or model call is involved.

    :param base_url: Spawned server's base URL.
    :param session_id: Session to attach the image to.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    upload = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/files",
        files={"file": (_IMAGE_NAME, base64.b64decode(_PNG_B64), "image/png")},
        timeout=30.0,
    )
    upload.raise_for_status()
    file_id = upload.json()["id"]

    SqlAlchemyConversationStore(str(_server_state["database_uri"])).append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_image",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_text", "text": "Here is the screenshot."},
                        {
                            "type": "input_image",
                            "file_id": file_id,
                            "filename": _IMAGE_NAME,
                        },
                    ],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_image",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": _REPLY}],
                    agent="hello_world",
                ),
            ),
        ],
    )


def _render(page: Page, base_url: str, session_id: str, *, serve_image: bool) -> dict:
    """Load the transcript and measure it, optionally blocking the image bytes.

    :param page: Playwright page.
    :param base_url: Spawned server's base URL.
    :param session_id: Session to open.
    :param serve_image: When ``False``, the file-content request is aborted so
        the image never decodes.
    :returns: The :data:`_PROBE` reading.
    """
    # Routes outlive a navigation, so a block left over from an earlier render
    # would silently starve this one.
    page.unroute_all()
    if not serve_image:
        page.route(re.compile(r"/resources/files/"), lambda route: route.abort())

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_REPLY)).to_be_visible(timeout=30_000)
    if serve_image:
        # Only a served image can reach complete=true; a blocked one settles as
        # soon as the request is aborted.
        page.wait_for_function(
            """() => {
                const img = document.querySelector('img[alt="shot.png"]');
                return img && img.complete && img.naturalWidth > 0;
            }""",
            timeout=30_000,
        )
    page.wait_for_timeout(500)
    return page.evaluate(_PROBE)


def test_image_preview_reserves_its_space(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The reply under an image sits on the same pixel loaded or not."""
    base_url, session_id = seeded_session
    _seed_image_turn(base_url, session_id)

    blocked = _render(page, base_url, session_id, serve_image=False)
    served = _render(page, base_url, session_id, serve_image=True)

    # The box is reserved, not derived from the bytes: an image that never
    # arrives still occupies the space one that does would have taken.
    assert blocked["boxHeight"] == served["boxHeight"], (blocked, served)
    # Guards the reservation itself — a collapsed box would agree at ~0.
    assert served["boxHeight"] > 200, served
    # The payoff: nothing below the image moves when the bytes land.
    assert blocked["replyTop"] == served["replyTop"], (blocked, served)
