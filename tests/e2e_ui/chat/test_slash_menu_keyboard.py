"""E2E: live slash-menu keyboard mechanics and the attachment open-gate.

The in-session composer gets its slash-menu mechanics — open condition,
highlight movement, Escape/arrow/Enter handling — from the shared
``useSlashCompletion`` hook. These tests drive a real browser against a
seeded session to pin the behaviors that hook owns on the live surface:

- ArrowUp/ArrowDown wrap the highlight around the match list.
- Enter completes the highlighted built-in (built-ins without arguments
  execute immediately).
- Escape clears the draft only while the menu has content; a plain
  non-command draft survives Escape.
- Pasting a file closes the open menu (the live menu's open condition
  requires an empty attachment list).

Selectors mirror the component: rows are ``data-testid="slash-menu-item-*"``
and the highlighted row carries ``data-active="true"`` (see
``SlashCommandMenu.tsx``).
"""

from playwright.sync_api import Page, expect

_ROWS = "[data-testid^='slash-menu-item-']"


def _composer(page: Page):
    return page.get_by_label("Message the agent")


def _open_menu(page: Page, composer) -> None:
    composer.fill("/")
    expect(page.locator(_ROWS).first).to_be_visible()


def test_arrow_navigation_wraps_highlight(page: Page, seeded_session: tuple[str, str]) -> None:
    """ArrowUp from the first match wraps to the last, ArrowDown wraps back."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(composer).to_be_visible(timeout=30_000)
    _open_menu(page, composer)

    rows = page.locator(_ROWS)
    assert rows.count() >= 2, "wrap navigation needs at least two matches"
    # A fresh query pre-selects the first match.
    expect(rows.first).to_have_attribute("data-active", "true")

    composer.press("ArrowUp")
    expect(rows.last).to_have_attribute("data-active", "true")
    composer.press("ArrowDown")
    expect(rows.first).to_have_attribute("data-active", "true")


def test_enter_completes_and_executes_the_highlighted_command(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Enter completes the highlighted built-in; argument-free built-ins execute."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(composer).to_be_visible(timeout=30_000)

    # "/ontext" substring-matches only /context, so Enter completes the
    # highlighted row. /context takes no argument, so selection executes it:
    # the draft clears and the output lands in the composer tray.
    composer.fill("/ontext")
    expect(page.get_by_test_id("slash-menu-item-context")).to_have_attribute("data-active", "true")
    composer.press("Enter")
    expect(composer).to_have_value("")
    expect(page.get_by_text("No usage data yet — send a message first.")).to_be_visible()


def test_escape_clears_the_draft_only_while_the_menu_has_content(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Escape clears a command draft with matches, but leaves plain text alone."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(composer).to_be_visible(timeout=30_000)

    _open_menu(page, composer)
    composer.press("Escape")
    expect(composer).to_have_value("")
    expect(page.locator(_ROWS)).to_have_count(0)

    # Content-gated: with the menu closed and no command token, Escape falls
    # through to the turn-cancel branch, which is a no-op while idle — the
    # draft must survive.
    composer.fill("hello")
    composer.press("Escape")
    expect(composer).to_have_value("hello")


# Playwright can't drive a real OS paste, but a page-built DataTransfer on a
# ClipboardEvent fires the same handler path the composer listens to.
_DISPATCH_FILE_PASTE = """
([selector, name, body]) => {
  const target = document.querySelector(selector);
  if (!target) throw new Error(`no paste target for ${selector}`);
  const transfer = new DataTransfer();
  transfer.items.add(new File([body], name, { type: "text/plain" }));
  return target.dispatchEvent(
    new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true })
  );
}
"""


def test_pasting_a_file_closes_the_slash_menu(page: Page, seeded_session: tuple[str, str]) -> None:
    """An attached file closes the live menu — its open gate is an empty attachment list."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(composer).to_be_visible(timeout=30_000)
    _open_menu(page, composer)

    page.evaluate(
        _DISPATCH_FILE_PASTE,
        ["textarea[aria-label='Message the agent']", "notes.txt", "hello"],
    )

    # The attachment lands as a chip, the menu closes, and the "/" draft
    # survives (the paste handler prevents the default text insert).
    expect(page.get_by_text("notes.txt")).to_be_visible()
    expect(page.locator(_ROWS)).to_have_count(0)
    expect(composer).to_have_value("/")
