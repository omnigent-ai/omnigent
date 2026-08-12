"""E2E: the image lightbox close button clears the iOS safe area.

The full-screen image lightbox positioned its close (x) button with a static
``top-3 right-3`` — a fixed 0.75rem from the viewport corner. Inside the native
iOS shell the WebView runs under ``viewport-fit=cover``, so that corner sits
under the status bar / Dynamic Island and the button renders unreachable on
notched iPhones. The fix offsets the button by the OS safe-area inset on top of
the original 0.75rem (``calc(var(--omnigent-safe-top, 0px) + 0.75rem)`` for the
top; ``env(safe-area-inset-right, 0px)`` for the right).

A jsdom component test can assert the inline style string but never that the
button actually moves — jsdom has no layout and no CSS ``calc``/``var``
resolution. This drives the real mechanism instead: the top offset reads
``--omnigent-safe-top`` (``max(env(safe-area-inset-top), ...)``), so overriding
the OS inset via CDP ``Emulation.setSafeAreaInsetsOverride`` must grow the
button's resolved ``top`` by exactly that inset. The old static ``top-3`` has no
``calc`` and would not move — which is the regression this guards. (A JS-set CSS
variable is not enough: this element resolves ``var(--omnigent-safe-top)`` at
computed-value time in a way that keeps the fallback, so only a genuine inset
moves it.)
"""

from __future__ import annotations

import base64

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

_IMAGE_NAME = "shot.png"
_REPLY = "Reviewing the screenshot."
_SAFE_TOP_PX = 44.0

# A real 240x180 PNG, inline so the fixture needs no image library — it just has
# to decode so the transcript renders a genuine zoomable image to click.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDA2GxtBgaDA0GBoMjaHB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)


def _seed_image_turn(base_url: str, session_id: str) -> None:
    """Attach an image to *session_id* and commit a turn that renders it.

    Uploads through the real file endpoint so the transcript's ``input_image``
    block resolves to genuine stored bytes, then writes the turn straight to the
    store — no agent turn or model call is involved.

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


def test_close_button_offset_by_safe_area_inset(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The lightbox close button moves down by the OS safe-area inset."""
    base_url, session_id = seeded_session
    _seed_image_turn(base_url, session_id)

    # A phone-sized viewport: env(safe-area-inset-*) — which the CDP override
    # below drives — only resolves under mobile metrics, and this is the iOS
    # scenario the fix targets anyway.
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_REPLY)).to_be_visible(timeout=30_000)

    # Open the full-screen lightbox by clicking the transcript's zoomable image.
    page.get_by_role("button", name=f"Zoom image: {_IMAGE_NAME}").click()
    close_button = page.get_by_role("button", name="Close")
    expect(close_button).to_be_visible(timeout=10_000)

    def resolved_top_px() -> float:
        return close_button.evaluate("el => parseFloat(getComputedStyle(el).top)")

    # With no safe area, the button keeps its base 0.75rem corner offset.
    baseline = resolved_top_px()
    assert baseline > 0, f"expected a positive base offset, got {baseline}"

    # Override the OS top inset the way a notched device reports it. The fix
    # folds env(safe-area-inset-top) into --omnigent-safe-top, so the button
    # must drop by exactly the inset; the old static top-3 would not move.
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Emulation.setSafeAreaInsetsOverride",
        {"insets": {"top": int(_SAFE_TOP_PX)}},
    )

    # The override re-lays-out asynchronously and the button eases into place
    # (Button carries transition-all, a fixed ~150ms ease), so let the shift
    # settle before sampling rather than catching it mid-animation.
    page.wait_for_timeout(400)
    with_inset = resolved_top_px()
    assert abs((with_inset - baseline) - _SAFE_TOP_PX) < 1.0, (
        f"close button did not shift by the safe-area inset: "
        f"baseline={baseline}, with_inset={with_inset}, expected delta={_SAFE_TOP_PX}"
    )
