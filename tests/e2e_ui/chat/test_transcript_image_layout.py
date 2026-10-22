"""Image attachments fit their contents and keep compact gaps when wrapping."""

from __future__ import annotations

import base64

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

_IMAGE_NAME = "shot.png"
_REPLY = "Reviewing the screenshot."

# A real 240x180 PNG, inline so the fixture needs no image library.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDI2hwdBgaDA0GBpDg6HB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)


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
                        {
                            "type": "input_image",
                            "file_id": file_id,
                            "filename": "second-shot.png",
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


def test_image_previews_wrap_with_compact_spacing(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Wrapped previews leave only the attachment gap, with no blank padding."""
    base_url, session_id = seeded_session
    _seed_image_turn(base_url, session_id)
    page.set_viewport_size({"width": 375, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_REPLY)).to_be_visible(timeout=30_000)
    page.wait_for_function(
        """() => {
            const images = [...document.querySelectorAll('img[alt$="shot.png"]')];
            return images.length === 2 &&
                images.every(img => img.complete && img.naturalWidth > 0);
        }""",
        timeout=30_000,
    )
    layout = page.evaluate(
        """() => {
            const images = [...document.querySelectorAll('img[alt$="shot.png"]')];
            const [first, second] = images.map(img => img.getBoundingClientRect());
            return {
                gap: second.top - first.bottom,
                heights: images.map(img => ({
                    image: img.getBoundingClientRect().height,
                    box: img.closest('div').getBoundingClientRect().height,
                })),
            };
        }"""
    )
    assert abs(layout["gap"] - 8) <= 1, layout
    for heights in layout["heights"]:
        assert 0 < heights["image"] <= 256, layout
        assert abs(heights["box"] - heights["image"]) <= 1, layout

    page.get_by_role("button", name="Zoom image: second-shot.png").click()
    expect(page.get_by_role("dialog")).to_be_visible()
