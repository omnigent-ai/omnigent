"""User-authored placeholders and HTML stay visible after send and reload."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Browser, expect

_MESSAGE = (
    "how about to reduce the output you can do like\n"
    "• ••\n"
    "<exact line(s) that needs to be seen without edit\n"
    "so for any matching line in the output which shows it, dont edit it or excerpt it, "
    "if any of the line shows important info\n\n"
    "Keep <exact lines> visible.\n\n"
    '<div class="example">\n  Keep this text\n  and this line\n</div>'
)


def test_user_message_preserves_literal_html(
    browser: Browser,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Send, reload, and copy the complete text, including raw HTML examples."""
    base_url, session_id = seeded_session
    context = browser.new_context(
        permissions=["clipboard-read", "clipboard-write"],
    )
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(_MESSAGE)
        page.get_by_role("button", name="Send", exact=True).click()

        bubble = page.locator('[data-testid="message-bubble"][data-role="user"]')
        expect(bubble).to_have_count(1)
        for line in _MESSAGE.splitlines():
            if line.strip():
                expect(bubble).to_contain_text(line.strip())

        expect(
            page.locator('[data-testid="message-bubble"][data-role="assistant"]')
        ).to_be_visible(timeout=90_000)
        page.reload()
        expect(bubble).to_have_count(1)
        for line in _MESSAGE.splitlines():
            if line.strip():
                expect(bubble).to_contain_text(line.strip())
        visible_lines = "\n".join(line.strip() for line in bubble.inner_text().splitlines())
        assert '<div class="example">\nKeep this text\nand this line\n</div>' in visible_lines

        bubble.get_by_role("button", name="Copy", exact=True).click()
        expect(bubble.locator("svg.lucide-check")).to_be_visible()
        assert page.evaluate("navigator.clipboard.readText()") == _MESSAGE
        bubble.screenshot(path=str(tmp_path / "user-message-literal-html.png"))
    finally:
        context.close()
