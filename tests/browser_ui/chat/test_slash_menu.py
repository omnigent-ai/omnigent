"""Browser-only contracts for native slash-menu interaction."""

import time

import pytest
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
    assert rows.count() >= 2, "wrap navigation needs at least two matches"
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


@pytest.mark.parametrize("phase", ["discovery", "runner-starting", "sandbox-starting"])
def test_open_menu_accepts_an_async_skill_catalog(
    page: Page, chat_session_contract, phase: str
) -> None:
    if phase != "discovery":
        chat_session_contract.set_health(runner_online=False, host_online=True)
        chat_session_contract.update_session(created_at=time.time())
    if phase == "sandbox-starting":
        chat_session_contract.update_session(sandbox_status={"stage": "provisioning"})
    chat_session_contract.set_skills(
        [{"name": "code-review", "description": "Review the current change"}]
    )
    release_skills = chat_session_contract.hold_skills()
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()

    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible()
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Skills unavailable while disconnected.", exact=True)
    ).not_to_be_visible()
    composer.fill("/review")
    expect(composer).to_have_value("/review")
    assert len(chat_session_contract.skill_requests) == 1

    release_skills()
    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible()
    skill = page.get_by_test_id("slash-menu-item-code-review")
    expect(skill).to_have_attribute("data-active", "true")
    composer.press("Tab")
    expect(composer).to_have_value("/code-review ")


def test_slash_menu_stops_loading_when_sandbox_launch_fails(
    page: Page, chat_session_contract
) -> None:
    chat_session_contract.set_health(runner_online=False, host_online=True)
    chat_session_contract.update_session(
        created_at=time.time(),
        host_id=None,
        workspace=None,
        sandbox_status={"stage": "provisioning"},
    )
    page.goto(chat_session_contract.url)
    chat_session_contract.wait_for_stream()
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/")
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()

    chat_session_contract.emit(
        {
            "event": "session.sandbox_status",
            "data": {
                "type": "session.sandbox_status",
                "conversation_id": chat_session_contract.session_id,
                "stage": "failed",
                "error": "Test sandbox could not start",
            },
        }
    )

    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible()
    expect(page.get_by_text("Skills unavailable while disconnected.", exact=True)).to_be_visible()


def test_read_only_composer_skips_skill_discovery(page: Page, chat_session_contract) -> None:
    chat_session_contract.update_session(permission_level=1)
    page.goto(chat_session_contract.url)

    expect(_composer(page)).to_be_disabled()
    assert chat_session_contract.skill_requests == []


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
