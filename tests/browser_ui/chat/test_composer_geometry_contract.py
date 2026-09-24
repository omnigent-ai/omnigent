"""Browser-only contract for composer, transcript, and responsive chat geometry."""

from __future__ import annotations

import re
from itertools import pairwise

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import (
    DESKTOP,
    PHONE,
    TABLET,
    TOLERANCE,
    assert_no_overlap,
    assert_row_grid,
    assert_same_vertical_center,
    assert_symmetric_insets,
    assert_within_viewport,
    box,
    drag_thumb_with_mouse,
    drag_thumb_with_touch,
    open_landing,
    open_live,
    park_at_bottom,
    right_inset,
    scroll_state,
    seed_items,
    seed_long_transcript,
    settled_geometry,
    tag_scroller,
)
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    list_payload,
    message_item,
    model_option,
)


def _surface(page: Page, chat: ChatSessionContract, name: str, viewport: dict[str, int]) -> None:
    if name == "landing":
        open_landing(page, chat, viewport)
    else:
        open_live(page, chat, viewport)


def _input(page: Page, surface: str):
    if surface == "landing":
        return page.get_by_test_id("new-chat-landing-input")
    return page.get_by_label("Message the agent")


def _action_row(page: Page, surface: str):
    if surface == "landing":
        return page.get_by_test_id("new-chat-landing-actions")
    return page.get_by_test_id("composer-action-row")


def _submit(page: Page, surface: str):
    if surface == "landing":
        return page.get_by_test_id("new-chat-landing-submit")
    return page.get_by_role("button", name="Send", exact=True)


def _attach(page: Page, names: list[str]) -> None:
    page.locator('input[type="file"]').first.set_input_files(
        [{"name": name, "mimeType": "text/plain", "buffer": b"geometry"} for name in names]
    )


def _agents(*, skills: bool = False) -> list[dict]:
    rows = []
    for agent_id, name, display, harness in [
        ("browser-chat-agent", "browser-chat-agent", "Browser agent", "claude-native"),
        ("browser-codex-agent", "browser-codex-agent", "Codex", "codex-native"),
        ("browser-gemini-agent", "browser-gemini-agent", "Gemini", "gemini-cli"),
    ]:
        rows.append(
            {
                "id": agent_id,
                "name": name,
                "display_name": display,
                "description": f"{display} agent",
                "harness": harness,
                "skills": (
                    [
                        {"name": "review-pr", "description": "Review a pull request"},
                        {"name": "cross-review", "description": "Cross-vendor review"},
                        {"name": "deslop", "description": "Remove AI slop"},
                    ]
                    if skills and agent_id == "browser-chat-agent"
                    else []
                ),
                "mcp_servers": [],
                "policies": [],
                "terminals": [],
            }
        )
    return rows


@pytest.mark.parametrize("viewport", [PHONE, TABLET, DESKTOP], ids=["phone", "tablet", "desktop"])
def test_landing_and_live_cards_share_the_geometry_contract(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """Both composer surfaces remain centered, bounded, and aligned at every breakpoint."""
    chat = chat_session_contract
    widths = []
    for surface in ("landing", "live"):
        _surface(page, chat, surface, viewport)
        card_locator = page.locator("[data-composer-card]")
        card = box(card_locator)
        container = box(card_locator.locator(".."))
        assert_symmetric_insets(card, container)
        assert_within_viewport(card, viewport["width"])
        assert_within_viewport(box(_action_row(page, surface)), viewport["width"])
        assert_within_viewport(box(_submit(page, surface)), viewport["width"])
        assert document_width(page) <= viewport["width"]
        widths.append(card["width"])
    if viewport == DESKTOP:
        assert widths[0] == pytest.approx(widths[1], abs=TOLERANCE)


def document_width(page: Page) -> int:
    return page.evaluate("() => document.documentElement.scrollWidth")


@pytest.mark.parametrize("surface", ["landing", "live"])
def test_content_and_action_controls_follow_shared_inset_lines(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
) -> None:
    chat = chat_session_contract
    _surface(page, chat, surface, DESKTOP)
    _attach(page, ["alpha.txt", "beta.txt"])
    card = box(page.locator("[data-composer-card]"))
    input_box = box(_input(page, surface))
    chip = box(page.get_by_role("button", name="Remove alpha.txt").locator(".."))
    if surface == "landing":
        leading = box(page.get_by_test_id("new-chat-landing-attach"))
        controls = [
            leading,
            box(page.get_by_test_id("new-chat-landing-agent-select")),
            box(_submit(page, surface)),
        ]
    else:
        leading = box(page.get_by_test_id("composer-attach"))
        controls = [
            leading,
            box(page.get_by_test_id("composer-config-gear")),
            box(_submit(page, surface)),
        ]
    expected_left = input_box["x"] - card["x"]
    assert chip["x"] - card["x"] == pytest.approx(expected_left, abs=TOLERANCE)
    assert leading["x"] - card["x"] == pytest.approx(expected_left, abs=TOLERANCE)
    assert right_inset(card, box(_submit(page, surface))) == pytest.approx(
        right_inset(card, input_box), abs=TOLERANCE
    )
    for left, right in pairwise(controls):
        assert_same_vertical_center(left, right)
        assert_no_overlap(left, right)


@pytest.mark.parametrize("surface", ["landing", "live"])
def test_label_collapse_preserves_submit_geometry(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
) -> None:
    chat = chat_session_contract
    if surface == "live":
        chat.set_catalog(
            harness="claude-native",
            models=[
                model_option(
                    "opus[1m]",
                    model="claude-opus-4-8[1m]",
                    display_name="Opus 4.8 (1M context)",
                    is_default=True,
                )
            ],
            selected_model="opus[1m]",
        )
    _surface(page, chat, surface, DESKTOP)
    card_locator = page.locator("[data-composer-card]")
    expanded_inset = right_inset(box(card_locator), box(_submit(page, surface)))
    page.set_viewport_size({"width": 280, "height": PHONE["height"]})
    expect(_action_row(page, surface)).to_have_attribute("data-labels", "collapsed")
    collapsed_inset = right_inset(box(card_locator), box(_submit(page, surface)))
    assert collapsed_inset == pytest.approx(expanded_inset, abs=TOLERANCE)
    leading = (
        page.get_by_test_id("new-chat-landing-attach")
        if surface == "landing"
        else page.get_by_test_id("composer-attach")
    )
    assert_same_vertical_center(box(leading), box(_submit(page, surface)))


@pytest.mark.parametrize("surface", ["landing", "live"])
def test_attachment_chips_follow_the_row_grid(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
) -> None:
    chat = chat_session_contract
    _surface(page, chat, surface, DESKTOP)
    if surface == "live":
        rail = page.get_by_role("complementary", name="Workspace")
        if rail.is_visible():
            page.keyboard.press("Control+Alt+BracketRight")
            expect(rail).not_to_be_visible()
    names = ["alpha.txt", "beta.txt", "gamma.txt"]
    _attach(page, names)
    chips = [
        box(page.get_by_role("button", name=f"Remove {name}").locator("..")) for name in names
    ]
    for chip in chips[1:]:
        assert chip["height"] == pytest.approx(chips[0]["height"], abs=TOLERANCE)
        assert_same_vertical_center(chips[0], chip)
    gaps = [
        chips[index + 1]["x"] - chips[index]["x"] - chips[index]["width"] for index in range(2)
    ]
    assert gaps[0] == pytest.approx(gaps[1], abs=TOLERANCE)
    assert chips[0]["x"] == pytest.approx(box(_input(page, surface))["x"], abs=TOLERANCE)


def test_wrapped_attachment_rows_keep_the_grid(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    open_live(page, chat, DESKTOP)
    names = [
        "quarterly-planning-notes.txt",
        "customer-feedback-export.csv",
        "incident-review-minutes.md",
        "onboarding-checklist-draft.txt",
        "competitive-analysis-sheet.csv",
        "roadmap-brainstorm-doc.md",
    ]
    _attach(page, names)
    chips = sorted(
        [box(page.get_by_role("button", name=f"Remove {name}").locator("..")) for name in names],
        key=lambda value: (value["y"], value["x"]),
    )
    lines: list[list[dict]] = []
    for chip in chips:
        if lines and abs(chip["y"] - lines[-1][0]["y"]) <= TOLERANCE:
            lines[-1].append(chip)
        else:
            lines.append([chip])
    assert len(lines) >= 2
    for line in lines:
        for chip in line[1:]:
            assert_same_vertical_center(line[0], chip)
    assert all(line[0]["x"] == pytest.approx(lines[0][0]["x"], abs=TOLERANCE) for line in lines)


def _register_agents(chat: ChatSessionContract, *, skills: bool = False) -> None:
    chat.contract.json("/v1/agents", list_payload(_agents(skills=skills)))


@pytest.mark.parametrize("surface", ["landing", "live"])
def test_slash_rows_follow_the_row_grid(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
) -> None:
    chat = chat_session_contract
    _register_agents(chat, skills=True)
    chat.harness = "claude-native"
    _surface(page, chat, surface, DESKTOP)
    input_locator = _input(page, surface)
    input_locator.click()
    input_locator.press_sequentially("/")
    rows = page.locator("[data-testid^='slash-menu-item-']")
    expect(rows.first).to_be_visible()
    boxes = [box(rows.nth(index)) for index in range(min(rows.count(), 3))]
    assert len(boxes) >= 2
    assert_row_grid(boxes)
    hovered = rows.nth(1)
    hovered.hover()
    assert hovered.evaluate("el => getComputedStyle(el).backgroundColor") not in {
        "rgba(0, 0, 0, 0)",
        "transparent",
    }


def test_mention_chips_follow_the_row_grid(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    chat.harness = "claude-native"
    names = ["alpha.md", "beta.md", "gamma.md"]
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/resources/environments/default",
        {"metadata": {"root": "/workspace"}},
    )
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/resources/environments/default/changes",
        list_payload([]),
    )
    chat.contract.json(
        re.compile(r"/v1/sessions/[^/]+/resources/environments/[^/]+/filesystem[?].*"),
        {
            "available": True,
            "data": [
                {"path": name, "name": name, "type": "file", "bytes": 10, "modified_at": None}
                for name in names
            ],
            "has_more": False,
        },
    )
    open_live(page, chat, DESKTOP)
    composer = page.get_by_label("Message the agent")
    for name in names:
        composer.press_sequentially("@")
        option = page.get_by_role("option", name=re.compile(name))
        expect(option).to_be_visible()
        option.get_by_role("button", name=name).click()
    chips = [box(page.locator(f"span[title='{name}']").locator("..")) for name in names]
    for chip in chips[1:]:
        assert_same_vertical_center(chips[0], chip)
    gaps = [
        chips[index + 1]["x"] - chips[index]["x"] - chips[index]["width"] for index in range(2)
    ]
    assert gaps[0] == pytest.approx(gaps[1], abs=TOLERANCE)


@pytest.mark.parametrize("surface", ["landing", "live"])
@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_picker_rows_follow_the_row_grid(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
    viewport: dict[str, int],
) -> None:
    chat = chat_session_contract
    _register_agents(chat)
    chat.set_catalog(
        harness="claude-native",
        models=[
            model_option("sonnet", model="claude-sonnet-5", display_name="Sonnet 5"),
            model_option(
                "opus[1m]",
                model="claude-opus-4-8[1m]",
                display_name="Opus 4.8 (1M context)",
                is_default=True,
            ),
            model_option("haiku", model="claude-haiku-4-5", display_name="Haiku 4.5"),
        ],
        selected_model="opus[1m]",
    )
    _surface(page, chat, surface, viewport)
    if surface == "landing":
        page.get_by_test_id("new-chat-landing-agent-select").click()
        rows = page.locator(".composer-agent-menu .composer-agent-row")
    else:
        page.get_by_test_id("composer-config-gear").click()
        page.get_by_test_id("composer-agent-edit").click()
        rows = page.locator(
            "[role^='menuitem'][data-testid^='composer-agent-model-']"
            ":not([data-testid='composer-agent-model-summary'])"
        )
    expect(rows.first).to_be_visible()
    rows.locator("xpath=ancestor::*[contains(@class, 'composer-agent-menu')][1]").first.evaluate(
        "el => Promise.all(el.getAnimations({subtree: true}).map(a => a.finished.catch(() => {})))"
    )
    values = [box(rows.nth(index)) for index in range(min(rows.count(), 3))]
    assert len(values) >= 2
    assert_row_grid(values)


@pytest.mark.parametrize("surface", ["landing", "live"])
def test_workspace_tray_aligns_with_the_card(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
) -> None:
    chat = chat_session_contract
    _surface(page, chat, surface, DESKTOP)
    if surface == "landing":
        tray = page.get_by_test_id("new-chat-landing-workspace-controls")
        chip = page.get_by_test_id("new-chat-landing-workspace-chip")
    else:
        tray = page.get_by_test_id("composer-workspace-controls")
        chip = page.get_by_test_id("composer-workspace-dir")
    tray_box = box(tray)
    card = box(page.locator("[data-composer-card]"))
    input_box = box(_input(page, surface))
    expected = input_box["x"] - card["x"]
    assert tray_box["x"] - card["x"] == pytest.approx(12, abs=TOLERANCE)
    assert right_inset(card, tray_box) == pytest.approx(12, abs=TOLERANCE)
    assert box(chip)["x"] - tray_box["x"] == pytest.approx(expected, abs=TOLERANCE)


def test_queued_rows_and_nested_workspace_share_the_tray_grid(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    open_live(page, chat, DESKTOP)
    chat.emit_busy("geometry-turn")
    composer = page.get_by_label("Message the agent")
    for message in ("queued one", "queued two"):
        composer.fill(message)
        page.get_by_role("button", name="Send", exact=True).click()
    strip = page.get_by_test_id("composer-queued-strip")
    expect(strip).to_be_visible()
    rows = strip.get_by_role("listitem")
    expect(rows).to_have_count(2)
    row_boxes = [box(rows.nth(0)), box(rows.nth(1))]
    assert row_boxes[0]["x"] == pytest.approx(row_boxes[1]["x"], abs=TOLERANCE)
    assert row_boxes[0]["width"] == pytest.approx(row_boxes[1]["width"], abs=TOLERANCE)
    assert row_boxes[0]["height"] == pytest.approx(row_boxes[1]["height"], abs=TOLERANCE)
    tray = page.get_by_test_id("composer-workspace-controls")
    assert tray.evaluate("el => getComputedStyle(el).borderTopWidth") == "0px"
    assert box(strip)["x"] == pytest.approx(box(tray)["x"], abs=TOLERANCE)


@pytest.mark.parametrize(
    ("viewport_width", "expected_frame", "expected_prose"),
    [(1920, 768, 768), (2400, 896, 768), (3200, 1280, 960), (4096, 1600, 1024)],
    ids=["desktop", "wide", "ultrawide", "4k"],
)
def test_chat_width_scales_while_prose_remains_readable(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport_width: int,
    expected_frame: float,
    expected_prose: float,
) -> None:
    chat = chat_session_contract
    seed_items(
        chat,
        list(
            reversed(
                [
                    message_item("user-prose", "user", "Show prose", response_id="prose"),
                    message_item(
                        "assistant-prose",
                        "assistant",
                        "Responsive prose width marker. " + "Readable sentence. " * 30,
                        response_id="prose",
                    ),
                    message_item("user-table", "user", "Show table", response_id="table"),
                    message_item(
                        "assistant-table",
                        "assistant",
                        "Responsive table width marker.\n\n"
                        "| View | Behavior |\n| --- | --- |\n| Prose | Readable |",
                        response_id="table",
                    ),
                ]
            )
        ),
    )
    open_live(page, chat, {"width": viewport_width, "height": 1080})
    expect(page.get_by_text("Responsive prose width marker.", exact=False)).to_be_visible()
    widths = page.evaluate(
        """() => {
          const frame = document.querySelector('.chat-conversation-content');
          const composer = document.querySelector('[data-composer-card]');
          const bubbles = [...document.querySelectorAll(
            '[data-testid="message-bubble"][data-role="assistant"]')];
          const prose = bubbles.find(b => b.textContent.includes('Responsive prose'));
          const table = bubbles.find(b => b.textContent.includes('Responsive table'));
          const style = getComputedStyle(frame);
          return {
            frame: frame.getBoundingClientRect().width,
            inner: frame.clientWidth
              - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
            prose: prose.getBoundingClientRect().width,
            table: table.getBoundingClientRect().width,
            composer: composer.getBoundingClientRect().width,
          };
        }"""
    )
    assert widths["frame"] == pytest.approx(expected_frame, abs=TOLERANCE)
    assert widths["prose"] == pytest.approx(expected_prose, abs=TOLERANCE)
    assert widths["table"] == pytest.approx(widths["inner"], abs=TOLERANCE)
    assert widths["composer"] == pytest.approx(expected_frame, abs=TOLERANCE)


def test_composer_fits_available_space_while_resizing(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    chat.seed_transcript(1)
    open_live(page, chat)
    for width in (3200, 1920, 1024, 390, 2400):
        page.set_viewport_size({"width": width, "height": 1080})
        dimensions = page.evaluate(
            """() => {
              const frame = document.querySelector('.chat-conversation-content');
              const card = document.querySelector('[data-composer-card]');
              const form = card.closest('form');
              const style = getComputedStyle(form);
              const rect = card.getBoundingClientRect();
              return {
                frame: frame.getBoundingClientRect().width,
                available: form.clientWidth
                  - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
                width: rect.width, left: rect.left, right: rect.right,
              };
            }"""
        )
        assert dimensions["width"] == pytest.approx(
            min(dimensions["frame"], dimensions["available"]), abs=TOLERANCE
        )
        assert dimensions["left"] >= 0 and dimensions["right"] <= width
        assert document_width(page) <= width


def test_composer_hides_native_scrollbar_without_disabling_scroll(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    seed_long_transcript(chat, 20)
    open_live(page, chat, {"width": 1280, "height": 720})
    composer = page.get_by_label("Message the agent")
    composer.fill("\n".join(f"Draft line {line}" for line in range(20)))
    geometry = page.evaluate(
        """() => {
          const composer = document.querySelector('textarea[aria-label="Message the agent"]');
          const transcript = document.querySelector('[role="log"] > div');
          const probe = el => ({
            clientHeight: el.clientHeight,
            scrollHeight: el.scrollHeight,
            scrollbarWidth: getComputedStyle(el).scrollbarWidth,
            webkitDisplay: getComputedStyle(el, '::-webkit-scrollbar').display,
          });
          return { composer: probe(composer), transcript: probe(transcript),
            root: getComputedStyle(document.documentElement).overflowY,
            body: getComputedStyle(document.body).overflowY };
        }"""
    )
    for surface in ("composer", "transcript"):
        assert geometry[surface]["scrollHeight"] > geometry[surface]["clientHeight"]
        assert geometry[surface]["scrollbarWidth"] == "none"
        assert geometry[surface]["webkitDisplay"] == "none"
    assert geometry["root"] == "hidden" and geometry["body"] == "hidden"
    composer.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    assert composer.evaluate("el => el.scrollTop") > 0


def test_composer_growth_reflows_without_covering_output(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    seed_long_transcript(chat, 20)
    open_live(page, chat, {"width": 1280, "height": 720})
    composer = page.get_by_label("Message the agent")
    composer.click()
    baseline = settled_geometry(page)
    for _ in range(3):
        composer.press("Shift+Enter")
    grown = settled_geometry(page)
    growth = grown["composerHeight"] - baseline["composerHeight"]
    assert growth > 0
    assert baseline["viewport"][0] - grown["viewport"][0] == pytest.approx(growth, abs=TOLERANCE)
    assert abs(grown["overlap"]) <= TOLERANCE
    assert grown["lastMessageBottom"] <= grown["composerTop"] + TOLERANCE
    assert grown["distanceFromBottom"] <= TOLERANCE
    composer.type("hello", delay=20)
    typed = settled_geometry(page)
    assert typed["composerHeight"] == grown["composerHeight"]
    assert typed["messageTops"] == grown["messageTops"]


def test_composer_growth_stays_bottom_pinned_every_frame(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    seed_long_transcript(chat, 20)
    open_live(page, chat, {"width": 1280, "height": 720})
    composer = page.get_by_label("Message the agent")
    composer.click()
    assert settled_geometry(page)["distanceFromBottom"] <= TOLERANCE
    distances = page.evaluate(
        """async () => {
          const ta = document.querySelector('textarea[aria-label="Message the agent"]');
          const scroller = ta.closest('form').parentElement.querySelector('[role="log"] > div');
          const samples = []; let sampling = true;
          const sample = () => { samples.push(
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop);
            if (sampling) requestAnimationFrame(sample); };
          requestAnimationFrame(sample);
          const setter = Object.getOwnPropertyDescriptor(
            HTMLTextAreaElement.prototype, 'value').set;
          for (let i = 0; i < 3; i += 1) {
            setter.call(ta, ta.value + '\\n');
            ta.dispatchEvent(new InputEvent('input', {bubbles: true}));
            await new Promise(resolve => setTimeout(resolve, 60));
          }
          sampling = false;
          await new Promise(resolve => setTimeout(resolve, 100));
          return samples;
        }"""
    )
    assert max(distances, default=0) <= TOLERANCE


def _single_reply(chat: ChatSessionContract, words: int) -> None:
    seed_items(
        chat,
        list(
            reversed(
                [
                    message_item("ghost-user", "user", "Summarize layout", response_id="ghost"),
                    message_item(
                        "ghost-assistant",
                        "assistant",
                        " ".join(f"word{index:04d}" for index in range(words)),
                        response_id="ghost",
                    ),
                ]
            )
        ),
    )


def _fully_visible_state(page: Page) -> dict:
    return page.evaluate(
        """() => {
          const el = document.querySelector('.transcript-hide-native-scrollbar');
          const thumb = document.querySelector('[data-testid="transcript-scrollbar-thumb"]');
          const messages = [...el.querySelectorAll('[data-role]')];
          const rect = el.getBoundingClientRect();
          return { max: el.scrollHeight - el.clientHeight, thumb: !!thumb,
            first: Math.round(messages[0].getBoundingClientRect().top),
            last: Math.round(messages.at(-1).getBoundingClientRect().bottom),
            top: Math.round(rect.top), bottom: Math.round(rect.bottom),
            width: Math.round(rect.width) };
        }"""
    )


def test_fully_visible_transcript_paints_no_scrollbar(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    _single_reply(chat, 140)
    open_live(page, chat, {"width": 1600, "height": 1000})
    expect(page.get_by_text("word0000", exact=False)).to_be_visible()
    state = _fully_visible_state(page)
    assert state["first"] >= state["top"] and state["last"] <= state["bottom"] - 32
    assert not state["thumb"], state


def test_panel_resize_does_not_summon_a_ghost_scrollbar(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    _single_reply(chat, 95)
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}",
        lambda _request: {**chat._session(), "workspace": "/workspace"},
    )
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/resources/environments/default",
        {"metadata": {"root": "/workspace"}},
    )
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/resources/environments/default/changes",
        list_payload([]),
    )
    chat.contract.json(
        re.compile(
            rf"/v1/sessions/{chat.session_id}/resources/environments/default/filesystem(?:\?.*)?$"
        ),
        list_payload([]),
    )
    chat.contract.json(
        re.compile(r"/v1/hosts/[^/]+/worktrees(?:\?.*)?$"),
        list_payload([]),
    )
    chat.contract.json("/v1/skills", list_payload([]))
    open_live(page, chat, {"width": 1600, "height": 1000})
    page.get_by_role("button", name="Expand right panel").click()
    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible()
    before = _fully_visible_state(page)
    handle = box(workspace.get_by_label("Resize panel"))
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    violations = []
    page.mouse.move(x, y)
    page.mouse.down()
    for delta in range(0, -380, -20):
        page.mouse.move(x + delta, y, steps=2)
        page.wait_for_timeout(60)
        state = _fully_visible_state(page)
        if state["thumb"] and state["max"] <= 24 and state["last"] <= state["bottom"] - 32:
            violations.append(state)
    page.mouse.up()
    after = _fully_visible_state(page)
    assert after["width"] < before["width"] - 40
    assert after["last"] > before["last"]
    assert after["last"] <= after["bottom"] - 32
    assert not after["thumb"] and not violations


def test_scrollbar_thumb_drags_by_touch(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    seed_long_transcript(chat, 30, paragraphs=6)
    open_live(page, chat, {"width": 800, "height": 1280})
    tag_scroller(page)
    expect(page.get_by_test_id("transcript-scrollbar-thumb")).to_be_visible()
    bottom = park_at_bottom(page)
    drag_thumb_with_mouse(page, -200)
    mouse_delta = bottom["scrollTop"] - scroll_state(page)["scrollTop"]
    assert mouse_delta > 200
    bottom = park_at_bottom(page)
    page.evaluate(
        """() => {
          const thumb = document.querySelector('[data-testid="transcript-scrollbar-thumb"]');
          window.__thumbEvents = [];
          for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel'])
            thumb.addEventListener(type, event => window.__thumbEvents.push(
              `${event.type}:${event.pointerType}`));
        }"""
    )
    drag_thumb_with_touch(page, -200)
    touch_delta = bottom["scrollTop"] - scroll_state(page)["scrollTop"]
    assert touch_delta > mouse_delta * 0.5, page.evaluate("() => window.__thumbEvents")
