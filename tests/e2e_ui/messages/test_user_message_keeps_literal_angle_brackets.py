"""E2E: user-authored angle-bracket text stays visible in the user bubble.

The user bubble renders message text as markdown (Streamdown). The renderer
parses HTML-like spans instead of showing them literally, so a placeholder
like ``<exact lines>`` vanishes from the rendered bubble and a pasted HTML
example loses its tags. Users expect their own text to remain visible as
literal text.

Each test sends one message through the real composer into a fresh session
and asserts the full literal text survives in the rendered user bubble.

Selectors:
  - composer: aria-label "Message the agent" (its placeholder mutates with
    turn state, so it is not a stable locator)
  - user bubble: ``data-testid="message-bubble"`` + ``data-role="user"``
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

_PLACEHOLDER_TEXT = "Keep <exact lines> visible."
_HTML_EXAMPLE_TEXT = '<div class="example">Keep this text</div>'


def _send_first_message(page: Page, base_url: str, session_id: str, text: str) -> None:
    """Open the session page, send ``text``, and wait for its user bubble."""
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER_BUBBLE)).to_have_count(1, timeout=15_000)


def test_user_message_keeps_angle_bracket_placeholder(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An ``<exact lines>`` placeholder stays visible as literal text.

    A failure means the markdown renderer swallowed the angle-bracket span
    from the user's own message: the bubble reads "Keep visible." instead of
    the text the user typed.
    """
    base_url, session_id = seeded_session

    _send_first_message(page, base_url, session_id, _PLACEHOLDER_TEXT)

    expect(page.locator(_USER_BUBBLE)).to_contain_text(_PLACEHOLDER_TEXT)


def test_user_message_keeps_html_example_tags(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A pasted HTML example keeps its tags visible as literal text.

    A failure means the markdown renderer treated the user's HTML example as
    markup: the tags disappear from the bubble and only the inner text (if
    anything) remains.
    """
    base_url, session_id = seeded_session

    _send_first_message(page, base_url, session_id, _HTML_EXAMPLE_TEXT)

    expect(page.locator(_USER_BUBBLE)).to_contain_text(_HTML_EXAMPLE_TEXT)
