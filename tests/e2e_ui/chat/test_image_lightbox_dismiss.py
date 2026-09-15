"""E2E: the image lightbox closes on a click beside the image, but not on a pan.

The preview used to be dismissible only by Escape or the "x": its stage fills
the viewport, so Radix never sees an "outside" click and the dialog swallowed
every click on the dark area around the image.

The stage now dismisses on its own, which is only safe because it can tell the
three gestures apart — a click beside the image closes, a click on the image
does not, and neither does the click a pan leaves behind when the pointer is
released. A component test drives synthetic events; only a browser produces a
real pan (pointer capture, a genuine click after the release) over an image
laid out at a real size, which is what makes this worth an e2e.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_PROMPT = "Here is the screenshot."

# 240x180 PNG — big enough that the stage keeps bare margins to click in.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDI2hwdBgaDA0GBpDg6HB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)


def _seed_image_turn(session_id: str) -> None:
    """Commit a user turn whose image opens in the lightbox.

    :param session_id: Session to append the turn to.
    """
    from omnigent.entities import MessageData, NewConversationItem

    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_lightbox",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_text", "text": _PROMPT},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{_PNG_B64}",
                        },
                    ],
                ),
            )
        ],
    )


def _open_lightbox(page: Page) -> None:
    """Open the preview and wait for its image to lay out."""
    page.get_by_role("button", name="Zoom image: Attached image").click()
    expect(page.get_by_role("dialog")).to_be_visible(timeout=10_000)
    page.wait_for_function(
        """() => {
            const img = document.querySelector('[role="dialog"] img');
            return img && img.getBoundingClientRect().width > 0;
        }""",
        timeout=10_000,
    )


def _boxes(page: Page) -> tuple[dict, dict]:
    """Return the preview image's box and the stage's box around it."""
    image = page.locator('[role="dialog"] img')
    image_box = image.bounding_box()
    stage_box = image.locator("xpath=..").bounding_box()
    assert image_box and stage_box
    return image_box, stage_box


def test_lightbox_closes_on_a_click_beside_the_image(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A click on the bare stage dismisses; one on the image does not."""
    base_url, session_id = seeded_session
    _seed_image_turn(session_id)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_PROMPT)).to_be_visible(timeout=30_000)

    _open_lightbox(page)
    image_box, stage_box = _boxes(page)
    # Vertically centred, so neither the "x" (top right) nor the zoom toolbar
    # (bottom centre) is under the cursor.
    bare_x = (stage_box["x"] + image_box["x"]) / 2
    bare_y = stage_box["y"] + stage_box["height"] / 2
    assert bare_x < image_box["x"], (stage_box, image_box)

    page.mouse.click(
        image_box["x"] + image_box["width"] / 2, image_box["y"] + image_box["height"] / 2
    )
    expect(page.get_by_role("dialog")).to_be_visible()

    page.mouse.click(bare_x, bare_y)
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)


def test_panning_a_zoomed_image_does_not_dismiss_the_lightbox(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A pan keeps the preview open; the next bare click still closes it."""
    base_url, session_id = seeded_session
    _seed_image_turn(session_id)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_PROMPT)).to_be_visible(timeout=30_000)

    _open_lightbox(page)
    page.get_by_role("button", name="Zoom in").click()
    expect(page.get_by_role("button", name="Reset zoom")).to_have_text("150%")

    image_box, stage_box = _boxes(page)
    start_x = image_box["x"] + image_box["width"] / 2
    start_y = image_box["y"] + image_box["height"] / 2

    # Drag the image left, releasing over the bare stage — the release's click
    # lands there and must not be read as a dismiss.
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(stage_box["x"] + 20, start_y, steps=10)
    page.mouse.up()
    expect(page.get_by_role("dialog")).to_be_visible()

    # A fresh click on the bare stage still closes it.
    page.mouse.click(stage_box["x"] + 20, stage_box["y"] + stage_box["height"] / 2)
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
