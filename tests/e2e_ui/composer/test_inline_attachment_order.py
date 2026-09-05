"""Browser proof for precise inline attachment placement in the composer."""

from __future__ import annotations

import base64

from playwright.sync_api import Locator, Page, expect

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


def _visible_parts(editor: Locator) -> list[str]:
    """Return the rendered text/attachment sequence from the first paragraph."""
    return editor.evaluate(
        """
        element => Array.from(element.querySelector('p').childNodes)
          .map(node => {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent;
            if (node instanceof HTMLElement && node.dataset.composerAttachment === 'true') {
              return node.querySelector('.truncate')?.textContent ?? '';
            }
            return node.textContent;
          })
          .filter(part => part !== '')
        """
    )


def test_picker_inserts_at_caret_and_attachment_can_move(
    page: Page,
    live_server: str,
) -> None:
    """The rendered order follows the caret, then updates after keyboard movement."""
    page.goto(f"{live_server}/")
    editor = page.get_by_test_id("new-chat-landing-input")
    expect(editor).to_be_visible(timeout=30_000)
    editor.fill("before after")
    editor.evaluate(
        """
        element => {
          const text = element.querySelector('p').firstChild;
          const range = document.createRange();
          range.setStart(text, 7);
          range.collapse(true);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
        }
        """
    )

    page.get_by_test_id("new-chat-landing-file-input").set_input_files(
        {
            "name": "middle.png",
            "mimeType": "image/png",
            "buffer": _ONE_PIXEL_PNG,
        }
    )

    expect(editor.locator('[data-composer-attachment="true"]')).to_have_count(1)
    assert _visible_parts(editor) == ["before ", "middle.png", "after"]

    editor.locator('[data-composer-attachment="true"]').click()
    for _ in "after":
        page.keyboard.press("Alt+ArrowRight")
    assert _visible_parts(editor) == ["before after", "middle.png"]
