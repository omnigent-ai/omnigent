"""Composer popovers stay between the composer and the transparent session header.

The header is an absolute ``z-30`` overlay, so anything that grows up into its box is
painted under the session title and icons. Each surface must stop at the header's bottom
edge, stack beneath it, and scroll whatever does not fit.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat._geometry_helpers import TOLERANCE, box
from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload

_SHORT_WINDOW = {"width": 1280, "height": 430}
_SKILLS = [
    {"name": f"repro-skill-{index:02d}", "description": f"Reproduction skill number {index:02d}"}
    for index in range(40)
]
_BACKGROUND_TASKS = [
    {
        "id": f"background-task-{index}",
        "type": "shell",
        "status": "running",
        "description": f"Background job {index}: watch the deploy pipeline and tail its logs",
        "command": f"gh run watch 100{index} && kubectl logs -f deploy/service-{index}",
    }
    for index in range(12)
]
_MULTILINE_DRAFT = "".join(f"Draft line {index}\n" for index in range(8))

# The outermost explicit z-index between a surface and the composer card decides how
# the surface stacks against the header.
_POPOVER_Z_INDEX = """
(surface) => {
  let zIndex = null;
  for (let el = surface; el && !el.hasAttribute("data-composer-card"); el = el.parentElement) {
    const value = getComputedStyle(el).zIndex;
    if (value !== "auto") zIndex = Number(value);
  }
  return zIndex;
}
"""


def _open(page: Page, chat: ChatSessionContract, viewport: dict[str, int]) -> Locator:
    page.set_viewport_size(viewport)
    page.goto(chat.url)
    chat.wait_for_stream()
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=20_000)
    return composer


def _header(page: Page) -> Locator:
    header = page.locator(".chat-header")
    expect(header).to_be_visible()
    return header


def _assert_below_header(page: Page, surface: Locator) -> None:
    header = box(_header(page))
    assert box(surface)["y"] >= header["y"] + header["height"] - TOLERANCE, (box(surface), header)


def _assert_stacks_under_header(page: Page, surface: Locator) -> None:
    surface_z = surface.evaluate(_POPOVER_Z_INDEX)
    header_z = _header(page).evaluate("el => getComputedStyle(el).zIndex")
    assert surface_z is not None and header_z != "auto", (surface_z, header_z)
    assert surface_z < int(header_z), (surface_z, header_z)


def _scroll_container(locator: Locator) -> Locator:
    return locator.locator("xpath=ancestor::*[contains(@class, 'overflow-y-auto')][1]")


def _assert_scrolls(scroller: Locator) -> None:
    client_height, scroll_height, overflow_y = scroller.evaluate(
        "el => [el.clientHeight, el.scrollHeight, getComputedStyle(el).overflowY]"
    )
    assert scroll_height > client_height and overflow_y == "auto", (
        client_height,
        scroll_height,
        overflow_y,
    )


def test_help_output_floats_below_header(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A long ``/help`` listing scrolls in a popover instead of growing the card."""
    chat = chat_session_contract
    chat.set_skills(_SKILLS)
    composer = _open(page, chat, {"width": 1280, "height": 720})
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-repro-skill-00")).to_be_visible()

    composer.fill("/help")
    composer.press("Enter")
    output = page.get_by_text("/repro-skill-00 — Reproduction skill number 00", exact=False)
    expect(output).to_be_visible()

    _assert_below_header(page, page.locator("[data-composer-card]"))
    _assert_below_header(page, output)
    _assert_stacks_under_header(page, output)
    _assert_scrolls(_scroll_container(output))
    expect(composer).to_have_value("")


def test_slash_menu_stays_below_header_on_short_window(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    chat.set_skills(_SKILLS)
    composer = _open(page, chat, _SHORT_WINDOW)
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-repro-skill-00")).to_be_visible()

    menu = page.get_by_test_id("slash-command-menu")
    _assert_below_header(page, menu)
    _assert_stacks_under_header(page, menu)
    _assert_scrolls(page.get_by_test_id("slash-menu-item-help").locator(".."))


@pytest.mark.parametrize("draft", ["", _MULTILINE_DRAFT], ids=["empty-draft", "multiline-draft"])
def test_mention_menu_stays_below_header_on_short_window(
    page: Page, chat_session_contract: ChatSessionContract, draft: str
) -> None:
    chat = chat_session_contract
    # Native coding-agent harnesses enable "@" mentions.
    chat.harness = "claude-native"
    environment = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(environment, {"metadata": {"root": "/workspace"}})
    chat.contract.json(f"{environment}/changes", list_payload([]))
    chat.contract.json(
        f"{environment}/filesystem",
        {
            "available": True,
            "data": [
                {"path": name, "name": name, "type": "file", "bytes": 1, "modified_at": None}
                for name in (f"file-{index:02d}.txt" for index in range(40))
            ],
            "has_more": False,
        },
    )
    composer = _open(page, chat, _SHORT_WINDOW)
    card = page.locator("[data-composer-card]")
    empty_card_height = box(card)["height"]
    composer.fill(draft)
    if draft:
        assert box(card)["height"] > empty_card_height + 100, (box(card), empty_card_height)
    composer.press_sequentially("@")
    expect(page.locator("[data-testid^='file-mention-item-']").first).to_be_visible()

    menu = page.get_by_test_id("file-mention-menu")
    _assert_below_header(page, menu)
    _assert_stacks_under_header(page, menu)
    _assert_scrolls(menu.get_by_role("listbox"))


def test_mcp_server_menu_stays_below_header_on_short_window(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    chat.harness = "claude-native"
    chat.contract.json(
        f"/v1/hosts/{chat.host_id}/mcp-servers",
        {
            "mcp_servers": [
                {
                    "name": f"server-{index:02d}",
                    "harness": "claude",
                    "transport": "http",
                    "scope": "user",
                    "url_host": f"mcp-{index:02d}.example.com",
                    "auth_status": "connected",
                }
                for index in range(30)
            ]
        },
    )
    composer = _open(page, chat, _SHORT_WINDOW)
    composer.fill("/mcp")
    menu = page.get_by_test_id("mcp-server-menu")
    first_server = menu.get_by_text("server-00", exact=True)
    expect(first_server).to_be_visible()

    _assert_below_header(page, menu)
    _assert_stacks_under_header(page, menu)
    _assert_scrolls(_scroll_container(first_server))


@pytest.mark.parametrize("native_safe_top", ["", "48px"], ids=["browser", "native-shell"])
def test_background_task_card_opens_below_header_on_short_window(
    page: Page, chat_session_contract: ChatSessionContract, native_safe_top: str
) -> None:
    chat = chat_session_contract
    _open(page, chat, _SHORT_WINDOW)
    if native_safe_top:
        # Native shells move the header down by the status-bar safe area.
        page.evaluate(
            """(safeTop) => {
              const root = document.documentElement;
              root.style.setProperty("--omnigent-android-safe-area-top", safeTop);
              document.querySelector(".app-shell").dataset.androidNative = "true";
            }""",
            native_safe_top,
        )
        assert box(_header(page))["y"] > 0, box(_header(page))
    chat.emit(
        {
            "event": "session.status",
            "data": {
                "conversation_id": chat.session_id,
                "status": "idle",
                "background_task_count": len(_BACKGROUND_TASKS),
                "background_tasks": _BACKGROUND_TASKS,
            },
        }
    )
    page.get_by_test_id("background-task-pill").click()

    panel = page.get_by_role("dialog", name=f"{len(_BACKGROUND_TASKS)} background tasks")
    expect(panel).to_be_visible()
    _assert_below_header(page, panel)
    _assert_scrolls(panel)
