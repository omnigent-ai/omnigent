"""UI e2e: the command palette leaves the caret in the destination composer.

This one only reproduces in a real browser. The palette is a modal dialog, so
Radix traps focus inside it and only releases the trap when the close animation
ends — several frames after the selection navigated away. A destination that
focuses its composer as it mounts therefore has that focus yanked back into the
dying dialog and dropped on ``<body>``. jsdom runs no animations, so a unit test
sees the trap release immediately and never catches it.

The journey under test is entirely keyboard-driven: ⌘K from a session, type,
Enter, then start typing the next prompt without touching the mouse.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# The new-session landing screen's composer.
_LANDING_INPUT = "new-chat-landing-input"
# The in-session composer, which carries no test id — match its placeholder,
# as the other composer e2e tests do.
_SESSION_COMPOSER = "Ask the agent anything…"

# Identifies whatever holds the caret: the landing composer's test id, or the
# in-session composer's placeholder.
_ACTIVE_COMPOSER_JS = """
() => {
  const el = document.activeElement;
  if (!el || el.tagName !== 'TEXTAREA') return null;
  return el.getAttribute('data-testid') ?? el.getAttribute('placeholder');
}
"""


def _wait_for_caret(page: Page, expected: str) -> None:
    """Wait for `expected` to hold the caret.

    Waiting rather than asserting immediately is what catches the bug: the
    destination focuses itself on mount and the dying palette steals it back a
    beat later, so an immediate assertion would pass on the broken build.
    """
    page.wait_for_function(f"() => ({_ACTIVE_COMPOSER_JS})() === {expected!r}", timeout=10_000)


def _wait_for_highlighted(page: Page, label: str) -> None:
    """Wait until `label` is the row Enter would run.

    The palette re-sorts around the debounced session search, so for a beat
    after the results swap there is no highlighted row and Enter is a no-op.
    Waiting for the highlight — rather than sleeping past the debounce and
    hoping — is what keeps the keypress landing on the intended row on a
    loaded machine.
    """
    page.wait_for_function(
        """(label) => {
             const el = document.querySelector('[cmdk-item][aria-selected="true"]');
             return !!el && el.textContent.trim() === label;
           }""",
        arg=label,
        timeout=10_000,
    )


def test_new_chat_from_palette_focuses_the_landing_composer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_SESSION_COMPOSER)).to_be_visible(timeout=30_000)

    page.keyboard.press("Control+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_visible(timeout=10_000)
    # "new chat" names the action outright, so the top result can't be a
    # session whose title happens to contain "new".
    palette.type("new chat", delay=20)
    # "New chat" has to be the highlighted row before Enter, or the keypress
    # lands in the gap where the debounced session results are swapping out.
    _wait_for_highlighted(page, "New chat")
    page.keyboard.press("Enter")

    expect(page.get_by_test_id(_LANDING_INPUT)).to_be_visible(timeout=15_000)
    _wait_for_caret(page, _LANDING_INPUT)

    # The whole point: type the next prompt with no click in between.
    page.keyboard.type("ship the thing")
    expect(page.get_by_test_id(_LANDING_INPUT)).to_have_value("ship the thing")


def test_sidebar_new_session_focuses_the_landing_composer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The sidebar link has no overlay in the way, but the caret must land the
    same place — this is the other route onto the new-session page."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_SESSION_COMPOSER)).to_be_visible(timeout=30_000)

    page.get_by_test_id("new-chat-button").click()

    expect(page.get_by_test_id(_LANDING_INPUT)).to_be_visible(timeout=15_000)
    _wait_for_caret(page, _LANDING_INPUT)


def test_session_switch_from_palette_focuses_the_session_composer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page.goto(base_url)
    expect(page.get_by_test_id(_LANDING_INPUT)).to_be_visible(timeout=30_000)

    # Sessions are the palette's first group, and an empty query lists the
    # recent ones. Click the row rather than pressing Enter: cmdk pins its
    # highlight to the first item that rendered, which is an action until the
    # session search resolves. Which input commits the row is beside the
    # point — the assertion is about focus after the palette closes.
    page.keyboard.press("Control+k")
    palette = page.get_by_role("dialog")
    expect(page.get_by_test_id("command-palette-input")).to_be_visible(timeout=10_000)
    # The seeded session is untitled, so it lists under the "New session"
    # fallback label (conversationDisplayLabel).
    palette.get_by_text("New session", exact=True).click(timeout=15_000)

    page.wait_for_url(f"**/c/{session_id}", timeout=15_000)
    _wait_for_caret(page, _SESSION_COMPOSER)
