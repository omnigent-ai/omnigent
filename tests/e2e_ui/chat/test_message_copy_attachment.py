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
text and the attachment chip land — the full round trip a user relies on.

The copy-side assertion avoids ``clipboard-read``/``clipboard-write``
permissions (contrast ``test_user_message_copy.py``, which grants them):
``navigator.clipboard.write`` is wrapped via ``page.add_init_script`` before
the SPA loads, recording each written item's MIME types before calling
through to the real implementation. Chromium's own clipboard-write
permission handling is irrelevant to what actually needs proving here — that
the app builds a `ClipboardItem` carrying both types.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_imported_inline_image import _INLINE_PNG
from tests.e2e_ui.conftest import seed_committed_items

_PROMPT = "Here is the pasted screenshot."
_COMPOSER_PLACEHOLDER = "Send a message…"
_COMPOSER_SELECTOR = '[aria-label="Message the agent"]'
_ATTACHED_NAME = "shot.png"

# Records what navigator.clipboard.write actually received, before Chromium's
# own clipboard permissioning (irrelevant here — see module docstring) can
# reject the call. An init script must be an IIFE (or bare statements): a
# bare arrow-function expression, unlike page.evaluate, is never called by
# add_init_script — it would just be created and discarded.
_RECORD_COPIED_TYPES = """
(() => {
  window.__omnigentCopiedTypes = null;
  if (!navigator.clipboard || !navigator.clipboard.write) return;
  const originalWrite = navigator.clipboard.write.bind(navigator.clipboard);
  navigator.clipboard.write = (items) => {
    window.__omnigentCopiedTypes = items[0] ? Array.from(items[0].types) : [];
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


def _seed_turn_with_inline_image(session_id: str) -> None:
    """Commit a user turn holding text plus an inline image block.

    :param session_id: Session to append the turn to.
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
                        {"type": "input_text", "text": _PROMPT},
                        {"type": "input_image", "image_url": _INLINE_PNG, "detail": "auto"},
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
    _seed_turn_with_inline_image(session_id)

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
