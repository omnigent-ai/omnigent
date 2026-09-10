"""E2E: copying a message's image attachment and pasting it back.

Two client-side changes have to work together for a copied prompt with an
image to be reusable: the composer's paste handler keeps pasted text
alongside pasted files (see ``test_composer_attachments.py``'s sibling
``lib/composerPaste.ts`` unit coverage), and the message bubble's Copy
button now puts the message's first image on the clipboard next to its text
(``lib/attachmentClipboard.ts``, ``lib/clipboard.ts``'s ``copyTextWithImage``).
Neither half is provable by a component test: writing a real
``ClipboardItem`` and reading ``clipboardData`` on a real ``paste`` event both
need a browser.

This drives both halves back-to-back: click Copy on a message with an inline
image and confirm the clipboard write carried both MIME types, then simulate
pasting that same (text, image) pair into the composer and confirm both the
text and the attachment chip land — the full round trip a user relies on. A
second test covers a JPEG attachment: ``toPngBlob``'s transcode path needs
``createImageBitmap``/``OffscreenCanvas``, which jsdom has neither of, so it
is only reachable here, in a real browser.

The copy-side assertion avoids ``clipboard-read``/``clipboard-write``
permissions (contrast ``test_user_message_copy.py``, which grants them):
``navigator.clipboard.write`` is wrapped via ``page.add_init_script`` before
the SPA loads, recording each written item's MIME types (and, for the
transcode test, the transcoded image's own bytes) before calling through to
the real implementation. Chromium's own clipboard-write permission handling
is irrelevant to what actually needs proving here — that the app builds a
``ClipboardItem`` carrying both types, and that its `image/png` bytes really
are PNG-encoded.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_imported_inline_image import _INLINE_PNG
from tests.e2e_ui.conftest import seed_committed_items

_PROMPT = "Here is the pasted screenshot."
_JPEG_PROMPT = "Here is a pasted JPEG screenshot."
_COMPOSER_PLACEHOLDER = "Send a message…"
_COMPOSER_SELECTOR = '[aria-label="Message the agent"]'
_ATTACHED_NAME = "shot.png"

# 1x1 red JPEG (Pillow: Image.new("RGB", (1, 1), (255, 0, 0)).save(buf, "JPEG",
# quality=50)) — a real JPEG, not a renamed PNG, so the transcode test proves
# toPngBlob actually re-encodes rather than passing bytes through unchanged.
_INLINE_JPEG = (
    "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERAT"
    "GCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDAR"
    "ESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2P/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBg"
    "cICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS"
    "0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dn"
    "d4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ"
    "2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8"
    "QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLR"
    "ChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6go"
    "OEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDFoooryz7w/9k="
)

# Records what navigator.clipboard.write actually received, before Chromium's
# own clipboard permissioning (irrelevant here — see module docstring) can
# reject the call. Also reads back the written image/png bytes' first 4 bytes
# via ClipboardItem.getType, which resolves the Blob independent of any
# clipboard permission — that call never touches the OS clipboard. An init
# script must be an IIFE (or bare statements): a bare arrow-function
# expression, unlike page.evaluate, is never called by add_init_script — it
# would just be created and discarded.
_RECORD_COPIED_TYPES = """
(() => {
  window.__omnigentCopiedTypes = null;
  window.__omnigentCopiedImageMagic = null;
  if (!navigator.clipboard || !navigator.clipboard.write) return;
  const originalWrite = navigator.clipboard.write.bind(navigator.clipboard);
  navigator.clipboard.write = (items) => {
    const item = items[0];
    window.__omnigentCopiedTypes = item ? Array.from(item.types) : [];
    if (item && item.types.includes("image/png")) {
      item
        .getType("image/png")
        .then((blob) => blob.arrayBuffer())
        .then((buf) => {
          window.__omnigentCopiedImageMagic = Array.from(new Uint8Array(buf).slice(0, 4));
        });
    }
    return originalWrite(items);
  };
})();
"""

# Synthesises a paste carrying both a file and plain text, mirroring what a
# real "copy message" clipboard write produces. Playwright can't drive the OS
# clipboard directly, but a page-built ClipboardEvent fires the same handler.
_DISPATCH_PASTE = """
([selector, text, filename, base64Png]) => {
  const target = document.querySelector(selector);
  if (!target) throw new Error(`no paste target for ${selector}`);
  const raw = atob(base64Png);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const file = new File([bytes], filename, { type: "image/png" });
  const dataTransfer = new DataTransfer();
  dataTransfer.items.add(file);
  dataTransfer.setData("text/plain", text);
  target.dispatchEvent(
    new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dataTransfer }),
  );
}
"""


def _seed_turn_with_inline_image(session_id: str, prompt: str, image_url: str) -> None:
    """Commit a user turn holding text plus an inline image block.

    :param session_id: Session to append the turn to.
    :param prompt: The message's text.
    :param image_url: The ``input_image`` block's inline data URI.
    """
    from omnigent.entities import MessageData, NewConversationItem

    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="codex:history",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url, "detail": "auto"},
                    ],
                ),
            ),
        ],
    )


def test_copy_message_with_image_then_paste_round_trips(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Copying a message with an image writes both MIME types; pasting them
    back into the composer restores the text and re-attaches the image."""
    base_url, session_id = seeded_session
    _seed_turn_with_inline_image(session_id, _PROMPT, _INLINE_PNG)

    page.add_init_script(_RECORD_COPIED_TYPES)
    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_PROMPT
    )
    expect(bubble).to_be_visible(timeout=30_000)

    copy_button = bubble.locator('[data-component-id="chat.message.copy_user"]')
    expect(copy_button).to_have_count(1)
    copy_button.click()

    page.wait_for_function(
        "() => window.__omnigentCopiedTypes !== null",
        timeout=10_000,
    )
    assert page.evaluate("() => window.__omnigentCopiedTypes") == ["text/plain", "image/png"]

    # The round trip: paste that same (text, image) pair into the composer.
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()

    base64_png = _INLINE_PNG.split(",", 1)[1]
    page.evaluate(_DISPATCH_PASTE, [_COMPOSER_SELECTOR, _PROMPT, _ATTACHED_NAME, base64_png])

    expect(page.get_by_role("button", name=f"Remove {_ATTACHED_NAME}")).to_be_visible(
        timeout=10_000
    )
    expect(composer).to_have_value(_PROMPT)


def test_copy_message_with_jpeg_attachment_transcodes_to_png(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A JPEG attachment is re-encoded to PNG before it reaches the clipboard.

    Chromium rejects a blob whose bytes don't match the MIME key it's
    announced under, so ``copyTextWithImage`` always writes its image under
    ``image/png`` — which only works if ``toPngBlob`` actually transcodes a
    non-PNG source rather than passing its bytes through unchanged. Checking
    the written MIME type alone can't tell the two apart; this reads the
    written bytes back and checks the PNG magic number.
    """
    base_url, session_id = seeded_session
    _seed_turn_with_inline_image(session_id, _JPEG_PROMPT, _INLINE_JPEG)

    page.add_init_script(_RECORD_COPIED_TYPES)
    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_JPEG_PROMPT
    )
    expect(bubble).to_be_visible(timeout=30_000)

    copy_button = bubble.locator('[data-component-id="chat.message.copy_user"]')
    expect(copy_button).to_have_count(1)
    copy_button.click()

    page.wait_for_function(
        "() => window.__omnigentCopiedImageMagic !== null",
        timeout=10_000,
    )
    assert page.evaluate("() => window.__omnigentCopiedTypes") == ["text/plain", "image/png"]

    magic = page.evaluate("() => window.__omnigentCopiedImageMagic")
    assert bytes(magic) == b"\x89PNG", (
        f"transcoded image did not start with the PNG magic number, got bytes {magic!r} "
        "— toPngBlob may not be re-encoding the JPEG source"
    )
