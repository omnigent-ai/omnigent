"""The chat's find bar: usable over floating banners, and it shows matches in collapsed content."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import DESKTOP, PHONE, assert_no_overlap, box
from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

# Every stream slot held by another holder: the chat opens over budget and
# raises the too-many-tabs banner, as when other tabs hold all the slots.
_HOLD_STREAM_SLOTS = """
for (let i = 0; i < 30; i += 1) {
  navigator.locks.request(`omnigent:stream-slot:${i}`, () => new Promise(() => {}));
}
"""
_COVERED_CONTROLS = """(bar) => [...bar.querySelectorAll('input, button')].filter((el) => {
  const r = el.getBoundingClientRect();
  return !el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
}).map((el) => el.getAttribute('aria-label'))"""


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_find_bar_stacks_above_the_too_many_tabs_banner(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """A banner stacks under the find bar instead of covering its controls."""
    chat = chat_session_contract
    chat.seed_transcript(4)
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/search",
        {"data": [], "has_more": False, "next_before": None},
    )
    page.set_viewport_size(viewport)
    page.add_init_script(_HOLD_STREAM_SLOTS)
    page.goto(f"{chat.url}?find=absent")

    bar = page.get_by_role("search")
    banner = page.get_by_role("status").filter(has_text="You have a lot of conversations open")
    expect(bar.get_by_text("No results")).to_be_visible(timeout=20_000)
    expect(banner).to_be_visible()

    assert bar.evaluate(_COVERED_CONTROLS) == []
    assert_no_overlap(box(bar), box(banner))
    assert box(bar)["y"] < box(banner)["y"]


# Highlight ranges a reader can see: inside the viewport and not clipped by an overflow box.
_VISIBLE_HIGHLIGHTS = """() => [...(CSS.highlights.get("chat-find") ?? [])].filter((range) => {
  const r = range.getBoundingClientRect();
  if (!r.width || !r.height || r.bottom <= 0 || r.top >= innerHeight) return false;
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const node = range.startContainer.parentElement;
  return !!hit && !!node && (hit === node || hit.contains(node) || node.contains(hit));
}).length"""


def _filler(name: str, turns: int) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index in range(turns):
        response_id = f"{name}-{index}"
        items += [
            message_item(
                f"{response_id}-user", "user", "Summarize the log.", response_id=response_id
            ),
            message_item(
                f"{response_id}-reply",
                "assistant",
                "The build passed. " * 80,
                response_id=response_id,
            ),
        ]
    return items


def _tool_turn(response_id: str, output: str) -> list[dict[str, object]]:
    call = {"response_id": response_id, "status": "completed", "call_id": f"{response_id}-call"}
    return [
        message_item(f"{response_id}-user", "user", "Run the checks.", response_id=response_id),
        {
            **call,
            "id": f"{response_id}-fc",
            "type": "function_call",
            "name": "sys_os_shell",
            "arguments": "{}",
        },
        {**call, "id": f"{response_id}-out", "type": "function_call_output", "output": output},
        message_item(
            f"{response_id}-reply", "assistant", "Checks passed.", response_id=response_id
        ),
    ]


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_find_steps_open_the_collapsed_content_that_holds_each_match(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """Every step shows its match highlighted on screen; closing the bar folds it all back."""
    chat = chat_session_contract
    long_prompt = "\n\n".join(f"Paragraph {n} of the pasted notes." for n in range(500))
    long_output = "\n".join(f"log line {n}" for n in range(150)) + "\nfinal: zorblat healthy"
    chronological = [
        *_filler("before", 3),
        *_tool_turn("tool-a", "service zorblat restarted"),
        *_filler("middle", 3),
        message_item(
            "prompt", "user", f"{long_prompt}\n\nThe zorblat flag.", response_id="prompt"
        ),
        message_item("prompt-reply", "assistant", "Noted.", response_id="prompt"),
        *_filler("later", 3),
        *_tool_turn("tool-b", long_output),
        *_filler("after", 3),
    ]
    chat.set_items(list(reversed(chronological)))
    matches = [("tool-a-out", "tool-a"), ("prompt", "prompt"), ("tool-b-out", "tool-b")]
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/search",
        {
            "data": [{"id": item_id, "response_id": rid} for item_id, rid in matches],
            "has_more": False,
            "next_before": None,
        },
    )
    page.set_viewport_size(viewport)
    page.goto(f"{chat.url}?find=zorblat")

    bar = page.get_by_role("search")
    for step in range(1, len(matches) + 1):
        expect(bar.get_by_text(f"{step} / {len(matches)}")).to_be_visible(timeout=20_000)
        page.wait_for_function(f"({_VISIBLE_HIGHLIGHTS})() > 0", timeout=10_000)
        if step < len(matches):
            bar.get_by_role("button", name="Next match").click()

    bar.get_by_role("button", name="Close find").click()
    expect(page.locator("[data-testid=turn-worked-fold][data-state=open]")).to_have_count(0)
    expect(page.get_by_text("Collapse prompt")).to_have_count(0)


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_find_lands_on_the_later_bubble_of_a_response_that_holds_the_match(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """A user message mid-response splits it into bubbles; the jump reaches the matching one."""
    chat = chat_session_contract
    long_output = "\n".join(f"log line {n}" for n in range(150)) + "\nfinal: zorblat healthy"
    first_half = _tool_turn("split", "all green")
    second_half = _tool_turn("split", long_output)
    for item in second_half:
        item["id"] = f"{item['id']}-2"
        if "call_id" in item:
            item["call_id"] = "split-call-2"
    chronological = [
        *_filler("before", 3),
        *first_half,
        message_item("steer", "user", "Keep going.", response_id="steer"),
        *second_half[1:],
        *_filler("after", 3),
    ]
    chat.set_items(list(reversed(chronological)))
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/search",
        {
            "data": [{"id": "split-out-2", "response_id": "split"}],
            "has_more": False,
            "next_before": None,
        },
    )
    page.set_viewport_size(viewport)
    page.goto(f"{chat.url}?find=zorblat")

    expect(page.get_by_role("search").get_by_text("1 / 1")).to_be_visible(timeout=20_000)
    page.wait_for_function(f"({_VISIBLE_HIGHLIGHTS})() > 0", timeout=10_000)
