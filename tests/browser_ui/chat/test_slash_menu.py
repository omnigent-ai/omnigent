"""Browser-only contracts for native slash-menu interaction."""

from playwright.sync_api import Page, expect

_ROWS = "[data-testid^='slash-menu-item-']"


def _composer(page: Page):
    return page.get_by_label("Message the agent")


def test_slash_menu_tracks_real_focus_and_wrapping_keyboard_navigation(
    page: Page, chat_session_contract
) -> None:
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/")

    rows = page.locator(_ROWS)
    expect(rows.first).to_have_attribute("data-active", "true")
    composer.press("ArrowUp")
    expect(rows.last).to_have_attribute("data-active", "true")
    composer.press("ArrowDown")
    expect(rows.first).to_have_attribute("data-active", "true")

    composer.blur()
    expect(rows).to_have_count(0)


def test_enter_executes_a_substring_matched_builtin(page: Page, chat_session_contract) -> None:
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()

    composer.fill("/ontext")
    context_row = page.get_by_test_id("slash-menu-item-context")
    expect(context_row).to_have_attribute("data-active", "true")
    composer.press("Enter")

    expect(composer).to_have_value("")
    expect(page.get_by_text("No usage data yet — send a message first.")).to_be_visible()


def test_native_file_paste_closes_the_slash_menu(page: Page, chat_session_contract) -> None:
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/")
    expect(page.locator(_ROWS).first).to_be_visible()

    page.evaluate(
        """
        () => {
          const target = document.querySelector("textarea[aria-label='Message the agent']");
          if (!target) throw new Error("composer not found");
          const transfer = new DataTransfer();
          transfer.items.add(new File(["hello"], "notes.txt", { type: "text/plain" }));
          target.dispatchEvent(
            new ClipboardEvent("paste", {
              clipboardData: transfer,
              bubbles: true,
              cancelable: true,
            }),
          );
        }
        """
    )

    expect(page.get_by_text("notes.txt")).to_be_visible()
    expect(page.locator(_ROWS)).to_have_count(0)
    expect(composer).to_have_value("/")
