"""Real-browser coverage for transcript rendering that depends on layout or chunks."""

from __future__ import annotations

import base64
import json
import re
from itertools import pairwise
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.browser_ui.chat._geometry_helpers import open_live, settled_geometry
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    list_payload,
    message_item,
    tool_call_items,
)

_CODE_BODY = '[data-streamdown="code-block-body"]'
_LONG_WORD = "horizontalScrolling" * 14
_SCROLL_CODE = "\n".join(f"const line{index} = '{_LONG_WORD}';" for index in range(60)) + "\n"
_TARGET = "https://example.com/docs/transcript"
_SEMICOLON_MERMAID = (
    "```mermaid\nsequenceDiagram\n"
    "    O->>O: Bind initiating user/run; check session policy\n"
    "    B->>B: Load grant#59; refresh if needed\n"
    "    G->>G: Record actor; target and outcome\n```"
)
_SEPARATOR_MERMAID = (
    "```mermaid\nsequenceDiagram\n"
    "    A->>B: first; B->>C: second\n"
    "    C->>D: punctuation; retry me\n```"
)
_INVALID_MERMAID = (
    "```mermaid\nsequenceDiagram\n"
    "    A->>B: hi\n"
    "    Note over A,B: proceed once; do not call Save\n"
    "    A=>B: again\n```"
)
_LONG_LABEL_MERMAID = """\
```mermaid
flowchart LR
  subgraph nonprod["Dev / staging account"]
    G["dev-aws-…-general1-vector-aggregator-role"]
    S["us-west-2-vector-aggregator-role (SMK)"]
  end
  subgraph audit["aws-audit (prod)"]
    W["MeterletK8sSecflowLogsWriterDev"]
    R["MeterletK8sSecflowLogsWriterDev-us-west-2"]
  end
  G -- AssumeRole --> W
  S -- AssumeRole --> R
  W -. "matches carve-out: no alert" .-> OK(("✓"))
  R -. "target ARN not in carve-out: alert" .-> SOC(("SA ticket"))
```"""
# Text of every Mermaid label whose laid-out lines spill outside its foreignObject box.
_CLIPPED_MERMAID_LABELS = """svg => [...svg.querySelectorAll('foreignObject')].flatMap(label => {
    const box = label.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(label);
    const clipped = [...range.getClientRects()].some(line => line.width && line.height && (
        line.left < box.left - 1 || line.right > box.right + 1
        || line.top < box.top - 1 || line.bottom > box.bottom + 1
    ));
    return clipped ? [label.textContent.trim()] : [];
})"""
_STREAMED_MERMAID_LINES = [
    "flowchart TD\n",
    "  A[Start] --> B[Read the config]\n",
    "  B --> C{Is it valid?}\n",
    "  C -->|yes| D[Run the job]\n",
    "  C -->|no| E[Report the error]\n",
]
# Per painted frame: whether the diagram's source and the diagram show, and the shown one's height.
_SAMPLE_DIAGRAM_FRAMES = """() => {
    window.__diagramFrames = [];
    const shown = (element) => !!element && element.checkVisibility({ visibilityProperty: true });
    const record = () => {
        const log = document.querySelector('[role="log"]');
        const source = [...log.querySelectorAll('[data-streamdown="code-block"]')]
            .find((block) => block.textContent.includes("flowchart TD"));
        const diagram = log.querySelector('[data-streamdown="mermaid-block"]');
        const visible = shown(diagram) ? diagram : shown(source) ? source : null;
        window.__diagramFrames.push({
            source: shown(source),
            diagram: shown(diagram),
            drawn: shown(log.querySelector('svg[id^="mermaid-"]')),
            height: visible ? Math.round(visible.getBoundingClientRect().height) : null,
        });
    };
    // Read layout after the app's resize observers have run, as the frame paints.
    const sample = () => {
        const observer = new ResizeObserver(() => {
            observer.disconnect();
            record();
        });
        observer.observe(document.body);
        requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
}"""
_POINTER_CONTEXTS = [
    pytest.param(
        True,
        marks=pytest.mark.browser_context_args(has_touch=True, is_mobile=True),
        id="touch",
    ),
    pytest.param(
        False,
        marks=pytest.mark.browser_context_args(has_touch=False, is_mobile=False),
        id="mouse",
    ),
]


def _seed_assistant_message(chat: ChatSessionContract, text: str) -> None:
    chat.set_items(
        [
            message_item(
                "transcript-rendering-assistant",
                "assistant",
                text,
                response_id="transcript-rendering-response",
            )
        ]
    )


@pytest.mark.parametrize(
    "saved_size", [None, 11, 13, 18], ids=["unset", "small", "saved-13", "large"]
)
@pytest.mark.parametrize("touch", _POINTER_CONTEXTS)
def test_message_font_size_survives_rotation_and_reload(
    page: Page,
    chat_session_contract: ChatSessionContract,
    saved_size: int | None,
    touch: bool,
) -> None:
    """Built message CSS follows the interface size through rotation and reload."""
    chat = chat_session_contract
    chat.set_items(
        [
            message_item(
                "font-assistant",
                "assistant",
                "Assistant prose with `inline code`.\n\n- A list item\n\n"
                "##### A small heading\n\n```text\ncode sample\n```",
                response_id="font-turn",
            ),
            message_item("font-user", "user", "User prose.", response_id="font-turn"),
        ]
    )
    viewports = (
        [(390, 844), (844, 390), (390, 844)] if touch else [(767, 844), (768, 844), (1280, 844)]
    )
    page.set_viewport_size({"width": viewports[0][0], "height": viewports[0][1]})
    page.goto(chat.url)
    if saved_size is not None:
        # Seed once, so reload cannot repair an accidentally changed preference.
        page.evaluate(
            "size => localStorage.setItem('omnigent:ui-font-size', String(size))", saved_size
        )
        page.reload()

    assistant = page.get_by_test_id("assistant-text-section")
    prose = [page.locator('[data-role="user"] p'), assistant.locator("p").first]
    prose.extend([assistant.locator("li"), assistant.locator('[data-streamdown="heading-5"]')])
    for width, height in viewports:
        page.set_viewport_size({"width": width, "height": height})
        for reload in (False, True):
            if reload:
                page.reload()
            assert page.evaluate("matchMedia('(pointer: coarse)').matches") is touch
            interface_px = saved_size if saved_size is not None else (15 if width < 768 else 14)
            message_px = interface_px
            for element in prose:
                expect(element).to_have_css("font-size", f"{message_px}px")
            expect(prose[1]).to_have_css("line-height", f"{message_px * 1.8:g}px")
            for selector in ('[data-streamdown="inline-code"]', _CODE_BODY):
                expect(assistant.locator(selector)).to_have_css(
                    "font-size", f"{message_px * 0.875:g}px"
                )
            expect(page.get_by_role("textbox", name="Message the agent", exact=True)).to_have_css(
                "font-size", f"{interface_px}px"
            )
            assert page.evaluate("localStorage.getItem('omnigent:ui-font-size')") == (
                None if saved_size is None else str(saved_size)
            )


@pytest.mark.parametrize("touch", _POINTER_CONTEXTS)
def test_large_message_fallback_keeps_message_font_size(
    page: Page, chat_session_contract: ChatSessionContract, touch: bool
) -> None:
    """Message fallbacks follow the mobile body tier and desktop caption tier."""
    text = "Long prose. " * 5_000 + "VISIBLE_FALLBACK_TAIL"
    chat = chat_session_contract
    chat.set_items(
        [
            message_item("large-assistant", "assistant", text, response_id="large-turn"),
            message_item("large-user", "user", text, response_id="large-turn"),
        ]
    )
    viewports = [(390, 844), (844, 390)] if touch else [(1280, 844)]
    page.set_viewport_size({"width": viewports[0][0], "height": viewports[0][1]})
    page.goto(chat.url)
    page.get_by_role("button", name=re.compile(r"^Show full prompt")).click()
    for width, height in viewports:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("matchMedia('(pointer: coarse)').matches") is touch
        for scope in ('[data-role="user"]', '[data-testid="assistant-text-section"]'):
            fallback = page.locator(f"{scope} .whitespace-pre-wrap.break-all")
            expect(fallback).to_contain_text("VISIBLE_FALLBACK_TAIL")
            expected_px = (15 if width < 768 else 14) if touch else 13
            expect(fallback).to_have_css("font-size", f"{expected_px}px")


@pytest.mark.only_browser("chromium")
@pytest.mark.browser_context_args(has_touch=False, is_mobile=False)
def test_custom_browser_font_default_matches_rendered_sizes_and_settings(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A larger browser default moves the rem breakpoint for both CSS and Settings."""
    initial_size = page.evaluate("parseInt(getComputedStyle(document.documentElement).fontSize)")
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Page.setFontSizes", {"fontSizes": {"standard": 20}})
        assert page.evaluate("getComputedStyle(document.documentElement).fontSize") == "20px"
        chat = chat_session_contract
        chat.set_items(
            [
                message_item(
                    "custom-assistant", "assistant", "Assistant prose.", response_id="custom"
                ),
                message_item("custom-user", "user", "User prose.", response_id="custom"),
            ]
        )
        page.set_viewport_size({"width": 800, "height": 844})
        page.goto(chat.url)
        assert not page.evaluate("matchMedia('(min-width: 48rem)').matches")
        assert not page.evaluate("matchMedia('(pointer: coarse)').matches")
        for selector in ('[data-role="user"] p', '[data-testid="assistant-text-section"] p'):
            expect(page.locator(selector)).to_have_css("font-size", "15px")
        expect(page.get_by_role("textbox", name="Message the agent", exact=True)).to_have_css(
            "font-size", "15px"
        )
        page.goto(f"{chat.contract.base_url}/settings/appearance")
        expect(page.get_by_test_id("ui-font-size-input")).to_have_value("15")
        expect(page.get_by_test_id("ui-font-size-input")).to_have_css("font-size", "15px")
        assert page.evaluate("localStorage.getItem('omnigent:ui-font-size')") is None
    finally:
        try:
            cdp.send("Page.setFontSizes", {"fontSizes": {"standard": initial_size}})
        finally:
            cdp.detach()


def _position_code_block(block: Locator) -> None:
    block.evaluate(
        """block => {
            const scroller = document.querySelector('[role="log"]').firstElementChild;
            scroller.scrollTop += block.getBoundingClientRect().top
                - scroller.getBoundingClientRect().top - 96;
        }"""
    )


def _assert_code_controls_follow_header(page: Page, block: Locator) -> None:
    _position_code_block(block)
    page.locator('[role="log"] > div').first.evaluate("el => { el.scrollTop += 200; }")
    page.wait_for_function(
        """() => {
            const block = document.querySelector('[data-streamdown="code-block"]').parentElement;
            const scroller = document.querySelector('[role="log"]').firstElementChild;
            const header = block.querySelector('[data-streamdown="code-block-header"]');
            const buttons = ['Toggle word wrap', 'Copy Code', 'Download file'].map(name =>
                [...block.querySelectorAll('button')].find(button =>
                    button.getAttribute('aria-label') === name || button.title === name
                )?.getBoundingClientRect()
            );
            if (buttons.some(rect => !rect)) return false;
            const headerRect = header.getBoundingClientRect();
            const headerCenter = headerRect.top + headerRect.height / 2;
            return headerRect.bottom < scroller.getBoundingClientRect().top
                && buttons.every(rect => Math.abs(rect.top + rect.height / 2 - headerCenter) < 2);
        }"""
    )


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_code_block_wrap_toggle_controls_horizontal_overflow(
    page: Page,
    chat_session_contract: ChatSessionContract,
    width: int,
) -> None:
    """Long code fits by default and overflows only when wrapping is disabled."""
    chat = chat_session_contract
    _seed_assistant_message(chat, f"```ts\n{_SCROLL_CODE}```")
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(chat.url)

    body = page.locator(_CODE_BODY).first
    block = page.locator('[data-streamdown="code-block"]').first.locator("..")
    toggle = page.get_by_role("button", name="Toggle word wrap")
    expect(body).to_be_visible(timeout=20_000)
    expect(toggle).to_have_attribute("aria-pressed", "true")
    page.wait_for_function(
        "selector => { const el = document.querySelector(selector); "
        "return !!el && el.scrollWidth - el.clientWidth <= 1; }",
        arg=_CODE_BODY,
    )

    _assert_code_controls_follow_header(page, block)

    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    page.wait_for_function(
        "selector => { const el = document.querySelector(selector); "
        "return !!el && el.scrollWidth - el.clientWidth > 1; }",
        arg=_CODE_BODY,
    )
    body.evaluate("el => { el.scrollLeft = 200; }")
    _assert_code_controls_follow_header(page, block)

    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    page.wait_for_function(
        "selector => { const el = document.querySelector(selector); "
        "return !!el && el.scrollWidth - el.clientWidth <= 1; }",
        arg=_CODE_BODY,
    )

    _position_code_block(block)
    scroller = page.locator('[role="log"] > div').first
    scroll_top = scroller.evaluate("el => el.scrollTop")
    with page.expect_download() as download_info:
        block.get_by_role("button", name="Download file").click()
    download = download_info.value
    assert download.suggested_filename.endswith(".ts")
    assert Path(download.path()).read_text() == _SCROLL_CODE
    assert abs(scroller.evaluate("el => el.scrollTop") - scroll_top) < 2
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_code_block_highlights_against_the_built_bundle(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The production Shiki chunks replace the raw fallback with colored tokens."""
    chat = chat_session_contract
    _seed_assistant_message(chat, "```ts\nconst answer = 42;\n```")
    page.goto(chat.url)

    tokens = page.locator(f'{_CODE_BODY} span[style*="--sdm-c"]')
    page.wait_for_function(
        """selector => {
            const spans = [...document.querySelectorAll(selector)];
            return spans.length > 1
                && new Set(spans.map(span => getComputedStyle(span).color)).size > 1;
        }""",
        arg=f'{_CODE_BODY} span[style*="--sdm-c"]',
        timeout=20_000,
    )
    expect(tokens.filter(has_text="const").first).to_be_visible()
    expect(tokens.filter(has_text="42").first).to_be_visible()


def test_mermaid_fence_loads_its_chunk_and_renders_svg(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The built SPA serves Mermaid's lazy chunk and renders every label inside its box."""
    chat = chat_session_contract
    _seed_assistant_message(chat, _LONG_LABEL_MERMAID)
    page.goto(chat.url)

    block = page.locator('[data-streamdown="mermaid-block"]').first
    expect(block).to_be_visible(timeout=20_000)
    diagram = block.locator("svg[aria-roledescription]")
    expect(diagram).to_be_visible(timeout=20_000)
    page.evaluate("() => document.fonts.ready")
    assert diagram.evaluate(_CLIPPED_MERMAID_LABELS) == []


@pytest.mark.parametrize("diagram", ["semicolons", "separators", "invalid"])
def test_mermaid_recovery_uses_the_real_browser_renderer(
    page: Page,
    chat_session_contract: ChatSessionContract,
    diagram: str,
) -> None:
    """Mermaid accepts escaped prose, preserves separators, and explains errors."""
    chat = chat_session_contract
    markdown = {
        "semicolons": _SEMICOLON_MERMAID,
        "separators": _SEPARATOR_MERMAID,
        "invalid": _INVALID_MERMAID,
    }[diagram]
    _seed_assistant_message(chat, markdown)
    page.goto(chat.url)

    if diagram == "invalid":
        card = page.get_by_test_id("mermaid-error")
        expect(card).to_be_visible(timeout=20_000)
        expect(card).to_contain_text("Mermaid couldn't parse line 3")
        expect(card.locator("pre > code")).to_have_text(
            "Note over A,B: proceed once; do not call Save"
        )
        expect(card.locator("details")).to_contain_text("got 'NEWLINE'")
        return

    escaped = page.get_by_test_id("mermaid-escaped")
    diagram_svg = escaped.locator("svg[aria-roledescription]")
    expect(diagram_svg).to_be_visible(timeout=20_000)
    if diagram == "semicolons":
        expect(diagram_svg.locator("text.messageText")).to_have_text(
            [
                "Bind initiating user/run; check session policy",
                "Load grant; refresh if needed",
                "Record actor; target and outcome",
            ]
        )
        expect(escaped).to_contain_text("2 semicolons escaped as #59; (first on line 2)")
        expect(escaped.locator("details")).to_contain_text("Load grant#59; refresh if needed")
    else:
        expect(diagram_svg.locator("text.messageText")).to_have_text(
            ["first", "second", "punctuation; retry me"]
        )
        expect(escaped).to_contain_text("one semicolon escaped as #59; (first on line 3)")
    expect(page.get_by_test_id("mermaid-error")).to_have_count(0)


def test_streaming_mermaid_shows_its_source_until_the_diagram_draws(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A Mermaid fence streams as code, and its diagram replaces the code once, already drawn.

    Drawing each partial source moved the text around it on every update, and an
    empty box and a spinner showed before each first drawing.
    """
    chat = chat_session_contract
    response_id = "streamed-diagram"
    user = message_item("streamed-diagram-user", "user", "Draw the flow.", response_id=response_id)
    chat.set_items([user])
    chat.update_session(status="running")
    open_live(page, chat)
    chat.emit_busy(response_id)
    chat.emit(
        {
            "event": "response.created",
            "data": {"id": response_id, "status": "in_progress", "output": []},
        }
    )
    chat.emit({"event": "response.output_text.delta", "data": {"delta": "Here is the flow:\n\n"}})
    expect(page.locator('[role="log"] p', has_text="Here is the flow")).to_be_visible(
        timeout=10_000
    )
    page.evaluate(_SAMPLE_DIAGRAM_FRAMES)
    for delta in ["```mermaid\n", *_STREAMED_MERMAID_LINES, "```\n\nThat's the flow.\n"]:
        chat.emit({"event": "response.output_text.delta", "data": {"delta": delta}})
        page.wait_for_timeout(300)
    expect(page.locator('[role="log"] svg[id^="mermaid-"]')).to_be_visible(timeout=10_000)
    page.wait_for_timeout(300)

    frames = page.evaluate("() => window.__diagramFrames")
    early = [frame["height"] for frame in frames if frame["diagram"] and not frame["drawn"]]
    assert not early, f"the diagram's box showed before it drew: {early}"
    shown = [frame for frame in frames if frame["source"] or frame["drawn"]]
    assert shown, "neither the source nor the diagram showed"
    after_first = frames[frames.index(shown[0]) :]
    assert all(frame["source"] or frame["drawn"] for frame in after_first), (
        "a frame showed neither the source nor the diagram"
    )
    kinds = [frame["diagram"] for frame in shown]
    assert kinds == sorted(kinds), "the source showed again after the diagram"
    source = [frame["height"] for frame in shown if not frame["diagram"]]
    diagram = [frame["height"] for frame in shown if frame["diagram"]]
    grew = len(set(source)) > 1 and source == sorted(source)
    assert grew, f"the source didn't grow as it streamed: {source}"
    assert max(diagram) - min(diagram) <= 1, f"the diagram changed size once shown: {diagram}"


@pytest.mark.parametrize("activation", ["click", "keyboard"])
def test_external_link_click_opens_the_rendered_target(
    page: Page,
    chat_session_contract: ChatSessionContract,
    activation: str,
) -> None:
    """A transcript link keeps its browser-level new-tab navigation contract."""
    chat = chat_session_contract
    _seed_assistant_message(chat, f"Read the [rendering guide]({_TARGET}).")
    requests: list[dict[str, str]] = []

    def fulfill_target(route: Route) -> None:
        requests.append(route.request.all_headers())
        route.fulfill(content_type="text/html", body="<title>Rendering guide</title>")

    page.context.route(
        _TARGET,
        fulfill_target,
    )
    page.goto(chat.url)

    link = page.get_by_role("link", name="rendering guide")
    expect(link).to_have_attribute("href", _TARGET)
    with page.expect_popup() as popup_info:
        if activation == "click":
            link.click()
        else:
            link.press("Enter")
    popup = popup_info.value
    popup.wait_for_url(_TARGET)
    expect(page).to_have_url(chat.url)
    assert popup.url == _TARGET
    assert popup.evaluate("window.opener === null")
    assert popup.evaluate("document.referrer") == ""
    assert requests and "referer" not in requests[0]
    assert len(page.context.pages) == 2


@pytest.mark.parametrize("activation", ["click", "keyboard"])
def test_external_link_falls_back_inside_a_popup_blocked_host(
    page: Page,
    chat_session_contract: ChatSessionContract,
    activation: str,
) -> None:
    """A sandboxed embed follows links in-frame without leaking its referrer."""
    chat = chat_session_contract
    _seed_assistant_message(chat, f"Read the [rendering guide]({_TARGET}).")
    requests: list[dict[str, str]] = []

    def fulfill_target(route: Route) -> None:
        requests.append(route.request.all_headers())
        route.fulfill(
            content_type="text/html",
            body="<title>Rendering guide</title><p>target-ok</p>",
        )

    page.context.route(_TARGET, fulfill_target)
    host_url = f"{chat.base_url}/popup-blocked-host"
    host_html = (
        '<!doctype html><html><body style="margin:0"><iframe id="embed" '
        'sandbox="allow-scripts allow-same-origin allow-forms" '
        f'src="{chat.url}" style="width:100vw;height:100vh;border:0"></iframe></body></html>'
    )
    page.route(
        host_url,
        lambda route: route.fulfill(status=200, content_type="text/html", body=host_html),
    )
    page.goto(host_url)

    frame = page.frame_locator("#embed")
    link = frame.get_by_role("link", name="rendering guide")
    expect(link).to_be_visible(timeout=20_000)
    if activation == "click":
        link.click()
    else:
        link.focus()
        link.press("Enter")

    body = frame.locator("body")
    expect(body).to_contain_text("target-ok", timeout=10_000)
    assert body.evaluate("el => el.ownerDocument.referrer") == ""
    assert requests and "referer" not in requests[0]
    expect(page).to_have_url(host_url)
    assert len(page.context.pages) == 1
    assert any(frame.url == _TARGET for frame in page.frames)


_CLOSING_LINE = "Short closing line for turn 2."
_DRAG_POINTS = """(line, releaseAt) => {
    const content = line.closest('.chat-conversation-content');
    let list = line;
    while (list.parentElement !== content) list = list.parentElement;
    let row = line;
    while (row.parentElement !== list) row = row.parentElement;
    const text = line.getBoundingClientRect();
    const y = text.top + text.height / 2;
    const start = [text.left + 1, y];
    if (releaseAt === 'right_margin') {
        const scroller = line.closest('[role="log"] > div').getBoundingClientRect();
        return {start, end: [(list.getBoundingClientRect().right + scroller.right) / 2, y]};
    }
    const next = row.nextElementSibling.getBoundingClientRect();
    return {start, end: [text.left + 40, (row.getBoundingClientRect().bottom + next.top) / 2]};
}"""


def _seed_pull_request_reviews(chat: ChatSessionContract) -> None:
    chronological: list[dict] = []
    for turn in (1, 2, 3):
        response_id = f"selection-response-{turn}"
        pull_requests = "\n".join(
            f"- [#{turn}0{number} Update docs](https://github.com/example/repo/pull/{turn}0{number})"
            for number in (1, 2, 3)
        )
        reply = (
            f"Turn {turn} summary: I checked the open pull requests for the review queue "
            "and wrote down what each one changes so you can decide which to merge first."
            f"\n\n{pull_requests}\n\nShort closing line for turn {turn}."
        )
        chronological += [
            message_item(
                f"selection-user-{turn}",
                "user",
                f"Check the open pull requests, round {turn}",
                response_id=response_id,
            ),
            message_item(
                f"selection-assistant-{turn}", "assistant", reply, response_id=response_id
            ),
        ]
    chat.set_items(list(reversed(chronological)))


@pytest.mark.parametrize("release_at", ["right_margin", "gap_below_message"])
def test_single_line_drag_selects_only_that_line(
    page: Page,
    chat_session_contract: ChatSessionContract,
    release_at: str,
) -> None:
    """A drag released beside or below a line does not extend the selection to other turns."""
    chat = chat_session_contract
    _seed_pull_request_reviews(chat)
    open_live(page, chat)
    line = page.get_by_text(_CLOSING_LINE, exact=True)
    line.scroll_into_view_if_needed()
    expect(line).to_be_visible()
    settled_geometry(page)

    points = line.evaluate(_DRAG_POINTS, release_at)
    page.mouse.move(*points["start"])
    page.mouse.down()
    page.mouse.move(*points["end"], steps=12)
    page.mouse.up()

    assert page.evaluate("() => window.getSelection().toString()").strip() == _CLOSING_LINE


def test_virtualized_transcript_returns_after_losing_layout(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Restoring a zero-size transcript keeps its current virtual rows visible."""
    chat = chat_session_contract
    chat.seed_transcript(60)
    open_live(page, chat)
    newest_reply = page.get_by_text("Turn 60 response with", exact=False)
    expect(newest_reply).to_be_visible(timeout=20_000)

    transcript = page.locator('[role="log"]')
    for _ in range(2):
        transcript.evaluate("element => { element.style.display = 'none'; }")
        expect(transcript).to_be_hidden()
        page.wait_for_timeout(100)  # Let ResizeObserver record the zero-size viewport.
        transcript.evaluate("element => { element.style.display = ''; }")
        page.wait_for_timeout(150)  # Let restored layout observers settle.
        expect(newest_reply).to_be_visible(timeout=5_000)


def _tall_topped_history() -> list[dict[str, object]]:
    """A short session whose two tallest messages sit just past the overscan at the bottom."""
    log = "\n".join(
        f"[ERROR] step {i}: cherry-pick conflict in build settings for dbr-branch-{i}"
        for i in range(48)
    )
    answer = "\n\n".join(
        f"Paragraph {i}: the backport needs the build-settings change from master "
        "before the 19.x job can pass, so both targets get the same fix."
        for i in range(13)
    )
    chronological = [
        message_item("tall-log", "user", log, response_id="tall-0"),
        message_item("tall-ack", "assistant", "Looking at that log now.", response_id="tall-0"),
        message_item("tall-ask", "user", "What does it say?", response_id="tall-1"),
        message_item("tall-answer", "assistant", answer, response_id="tall-1"),
    ]
    for n in range(6):
        response_id = f"short-{n}"
        reply = "Done." if n % 2 else "Checked both branches; the master audit still passes."
        chronological += [
            message_item(
                f"{response_id}-ask",
                "user",
                f"Follow-up {n}: keep going.",
                response_id=response_id,
            ),
            message_item(f"{response_id}-reply", "assistant", reply, response_id=response_id),
        ]
    return list(reversed(chronological))


def _wheel(page: Page, total: float, steps: int = 40) -> None:
    """A trackpad-like fling: front-loaded wheel deltas that taper off."""
    for i in range(steps):
        page.mouse.wheel(0, total / steps * 2.0 * (steps - i) / steps)
        page.wait_for_timeout(8)


def test_reduced_motion_transcript_holds_still_at_the_bottom(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """With reduced motion, the bottom of a live turn doesn't flip between rows every frame."""
    page.emulate_media(reduced_motion="reduce")
    chat = chat_session_contract
    chat.set_items(_tall_topped_history())
    chat.update_session(status="running")
    open_live(page, chat, {"width": 1240, "height": 1051})
    chat.emit_busy("tall-live")
    expect(page.get_by_test_id("working-indicator")).to_be_visible(timeout=20_000)

    transcript = page.locator("[data-virtualized-transcript]")
    # Rows mount and unmount at once, so the padding that stands in for them must too.
    applied = transcript.evaluate(
        """el => {
          const list = el.querySelector('[data-index]').parentElement;
          const before = list.style.paddingTop;
          list.style.paddingTop = '123px';
          const value = getComputedStyle(list).paddingTop;
          list.style.paddingTop = before;
          return value;
        }"""
    )
    assert applied == "123px"

    box = transcript.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] * 0.85, box["y"] + box["height"] * 0.7)
    _wheel(page, -1200)
    page.wait_for_timeout(300)
    _wheel(page, 3000)
    page.mouse.move(box["x"] + 5, box["y"] + 5)
    heights = transcript.evaluate(
        """el => new Promise((resolve) => {
          const heights = [];
          const tick = () => {
            heights.push(el.scrollHeight);
            if (heights.length < 90) requestAnimationFrame(tick);
            else resolve(heights);
          };
          requestAnimationFrame(tick);
        })"""
    )
    changes = sum(1 for before, after in pairwise(heights) if before != after)
    assert changes <= 2, heights


# Records, once per painted frame, the top of the answer paragraph carrying ``text``.
_TRACK_ANSWER_TOP = """
(text) => {
  window.__answerTops = [];
  const frame = () => {
    const observer = new ResizeObserver(() => {
      observer.disconnect();
      const answer = [...document.querySelectorAll('[data-testid="message-bubble"] p')].find(
        (p) => (p.textContent || '').includes(text));
      if (answer) window.__answerTops.push(answer.getBoundingClientRect().top);
    });
    observer.observe(document.documentElement);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
"""


@pytest.mark.parametrize("reduced_motion", [False, True], ids=["animated", "reduced-motion"])
def test_worked_fold_forms_without_jolting_the_answer(
    page: Page,
    chat_session_contract: ChatSessionContract,
    reduced_motion: bool,
) -> None:
    """The fold that forms over a finished turn's trace moves the answer only toward its rest.

    The fold mounts open over the trace and closes. It used to mount with the trace
    indented and padded under a row that could not shrink below its padding, so the
    answer below dropped about 16px in one frame and slid back during the collapse.
    """
    chat = chat_session_contract
    response_id = "fold-in-place"
    answer = "The repo has one README and nothing else."
    chronological = [
        message_item("fold-in-place-user", "user", "What's in the repo?", response_id=response_id),
        *tool_call_items(
            "call-fold-in-place",
            "shell",
            {"command": "ls"},
            "README.md\n",
            response_id=response_id,
        ),
        message_item("fold-in-place-answer", "assistant", answer, response_id=response_id),
    ]
    chat.set_items(list(reversed(chronological)))
    chat.update_session(status="running")
    if reduced_motion:
        page.emulate_media(reduced_motion="reduce")
    open_live(page, chat)
    chat.emit_busy(response_id)
    expect(page.get_by_test_id("working-indicator")).to_be_visible(timeout=20_000)
    bubble = page.locator('[data-testid="message-bubble"]', has=page.get_by_text(answer)).first
    expect(bubble).to_be_visible()
    page.evaluate(_TRACK_ANSWER_TOP, answer)

    chat.emit_idle(response_id)
    expect(bubble.get_by_test_id("turn-worked-fold")).to_have_count(1, timeout=15_000)
    # The trace's content is hidden once its collapse animation ends.
    expect(bubble.locator(".turn-fold-content")).to_be_hidden(timeout=15_000)
    tops = page.evaluate("() => window.__answerTops")
    before, rest = tops[0], tops[-1]
    low, high = min(before, rest) - 1, max(before, rest) + 1
    assert all(low <= top <= high for top in tops), (
        f"the answer moved past where it settled ({before:.1f} -> {rest:.1f}): "
        f"{[round(top, 1) for top in tops]}"
    )


def test_streamed_reply_stays_mounted_as_its_turn_commits_and_ends(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A streamed reply keeps its elements, and the reader's selection, through its turn.

    The relay commits the tool call that closed the text, then stamps the text's id,
    and the turn ends and folds its trace. None of these may rebuild the reply's markdown.
    """
    chat = chat_session_contract
    response_id = "mounted-reply"
    text = "I'll read the file first. The names are stored one per line."
    user = message_item("mounted-reply-user", "user", "Read the file.", response_id=response_id)
    first = message_item("mounted-reply-text", "assistant", text, response_id=response_id)
    call, output = tool_call_items(
        "call-mounted-reply",
        "shell",
        {"command": "cat names.txt"},
        "ada\n",
        response_id=response_id,
    )
    chat.set_items([user])
    chat.update_session(status="running")
    open_live(page, chat)
    chat.emit_busy(response_id)
    chat.emit(
        {
            "event": "response.created",
            "data": {"id": response_id, "status": "in_progress", "output": []},
        }
    )
    # The stream holds back a trailing partial word, so match the words already shown.
    chat.emit({"event": "response.output_text.delta", "data": {"delta": text}})
    first_paragraph = page.locator('[role="log"] p', has_text="stored one per")
    expect(first_paragraph).to_be_visible(timeout=10_000)
    first_paragraph.evaluate("(p) => { window.__first = p; getSelection().selectAllChildren(p); }")

    for item in (call, first, output):
        chat.emit({"event": "response.output_item.done", "data": {"item": item}})
    answer = "Done. There are two names in the file."
    chat.emit({"event": "response.output_text.delta", "data": {"delta": answer}})
    last_paragraph = page.locator('[role="log"] p', has_text="two names in")
    expect(last_paragraph).to_be_visible(timeout=10_000)
    last_paragraph.evaluate("(p) => { window.__last = p; }")
    chat.emit(
        {
            "event": "response.completed",
            "data": {"id": response_id, "status": "completed", "output": []},
        }
    )
    chat.emit_idle(response_id)
    expect(page.get_by_test_id("working-indicator")).to_be_hidden(timeout=10_000)
    expect(page.get_by_test_id("turn-worked-fold")).to_have_count(1, timeout=15_000)
    expect(page.locator(".turn-fold-content")).to_be_hidden(timeout=15_000)

    assert first_paragraph.evaluate("(p) => p === window.__first"), "the first message was rebuilt"
    assert last_paragraph.evaluate("(p) => p === window.__last"), "the answer was rebuilt"
    assert page.evaluate(
        "() => !getSelection().isCollapsed && getSelection().containsNode(window.__first, true)"
    ), "the reader's selection was cleared"


_ISSUE_TITLE = "Claude mod replaces tmux paste for native turns"
_PR_ERROR = "Error: 422 Validation Failed: A pull request already exists"
_ISSUE_DESCRIPTION = (
    "## Context\n\n"
    "Follow-up to [#11607](<https://github.com/omnigent-ai/omnigent/pull/11607>).\n\n"
    "1. Show added and removed line counts.\n2. Show display names for MCP servers."
)

_LONG_TOOL_PATH = "/workspace/" + "long_directory/" * 12 + "notes.md"


def _seed_tool_calls(chat: ChatSessionContract) -> None:
    """One settled turn with a plan, MCP outcomes, a long read and a counted edit."""
    response_id = "tool-rows-response"
    issue = {
        "id": "OMNI-12243",
        "title": _ISSUE_TITLE,
        "state": "In Review",
        "description": _ISSUE_DESCRIPTION,
    }
    todo_list = [
        {"id": 1, "title": "Find review workflow", "status": "completed"},
        {"id": 2, "title": "Fetch open PRs", "status": "in-progress"},
    ]
    chronological = [
        message_item("tool-rows-user", "user", "Plan, then open the PR.", response_id=response_id),
        *tool_call_items(
            "call-plan",
            "manage_todo_list",
            {"operation": "write", "todoList": todo_list},
            "Task plan updated (2 tasks).",
            response_id=response_id,
        ),
        # Claude-native stores MCP results as content blocks around the server's JSON.
        *tool_call_items(
            "call-issue",
            "mcp__linear__get_issue",
            {
                "id": "OMNI-12243",
                "includeComments": False,
                "fields": ["id", "title", "description", "state", "labels"],
            },
            json.dumps([{"type": "text", "text": json.dumps(issue)}]),
            response_id=response_id,
        ),
        *tool_call_items(
            "call-pr",
            "mcp__github__create_pull_request",
            {"owner": "omnigent-ai", "repo": "omnigent", "draft": True},
            _PR_ERROR,
            response_id=response_id,
        ),
        *tool_call_items(
            "call-read",
            "Read",
            {"file_path": _LONG_TOOL_PATH},
            "# File opened from a tool row",
            response_id=response_id,
        ),
        *tool_call_items(
            "call-long-edit",
            "Edit",
            {
                "file_path": _LONG_TOOL_PATH,
                "old_string": "old line\n",
                "new_string": "new line\nsecond line\n",
            },
            f"The file {_LONG_TOOL_PATH} has been updated successfully.",
            response_id=response_id,
        ),
        message_item(
            "tool-rows-assistant", "assistant", "The PR already exists.", response_id=response_id
        ),
    ]
    chat.set_items(list(reversed(chronological)))


@pytest.mark.parametrize(
    ("width", "app_kind"),
    [
        pytest.param(1280, None, id="desktop"),
        pytest.param(390, None, id="mobile-browser"),
        pytest.param(
            320,
            "android",
            id="android-app-320",
            marks=pytest.mark.browser_context_args(has_touch=True, is_mobile=True),
        ),
        pytest.param(
            390,
            "ios",
            id="ios-app-390",
            marks=pytest.mark.browser_context_args(has_touch=True, is_mobile=True),
        ),
    ],
)
def test_tool_rows_read_as_actions_with_their_outcome(
    page: Page,
    chat_session_contract: ChatSessionContract,
    width: int,
    app_kind: str | None,
) -> None:
    """Tool rows show a humanized title and MCP server, with details behind the expand."""
    chat = chat_session_contract
    _seed_tool_calls(chat)
    environment_api = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(environment_api, {"metadata": {"root": "/browser-workspace"}})
    for resource in ("changes", "filesystem"):
        chat.contract.json(f"{environment_api}/{resource}", list_payload([]))
    chat.contract.json(f"/v1/sessions/{chat.session_id}/comments", [])
    if app_kind:
        page.add_init_script(f"window.omnigentNative = {json.dumps({'kind': app_kind})}")
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(chat.url)

    expect(page.get_by_text("The PR already exists.")).to_be_visible(timeout=20_000)
    worked = page.get_by_role("button", name=re.compile(r"^Worked"))
    expect(worked).to_have_attribute("aria-expanded", "false")
    worked.click()
    page.get_by_text("Read 1 file, edited 1 file, called 3 other tools", exact=True).click()

    plan = page.get_by_role("button", name=re.compile(r"^Manage todo list"))
    expect(plan).to_contain_text("write · 2 items")
    expect(plan).not_to_contain_text("Task plan updated")
    issue = page.get_by_role("button", name=re.compile(r"^Get issue"))
    # Known MCP servers carry their brand spelling, not the config name.
    expect(issue.get_by_text("Linear", exact=True)).to_be_visible()
    # The list of fields to return is not the subject of the call.
    expect(issue).not_to_contain_text("items")
    expect(issue).not_to_contain_text(_ISSUE_TITLE)
    failure = page.get_by_role("button", name=re.compile(r"^Create pull request"))
    expect(failure.get_by_text("GitHub", exact=True)).to_be_visible()
    expect(page.get_by_text(_PR_ERROR, exact=True)).not_to_be_visible()
    failure.click()
    expect(failure.locator("xpath=..").get_by_test_id("tool-error-box")).to_have_text(_PR_ERROR)
    failure.click()
    expect(page.get_by_text(_PR_ERROR, exact=True)).not_to_be_visible()

    transcript = page.locator('[role="log"]')
    expect(transcript).not_to_contain_text("mcp__linear__")
    expect(transcript).not_to_contain_text('{"operation"')
    # Each row is one line, and long rows truncate rather than wrap or widen the page.
    height = "el => el.getBoundingClientRect().height"
    heights = [row.evaluate(height) for row in (plan, issue, failure)]
    assert max(heights) - min(heights) < 1, heights
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Rows fill the reply's width even though its text is one short line.
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    right_edge = "el => el.getBoundingClientRect().right"
    assert reply.evaluate(right_edge) - plan.evaluate(right_edge) < 2

    issue.click()
    card = issue.locator("xpath=..")
    fields = card.get_by_test_id("tool-value-view")
    expect(fields.last).to_contain_text(f"Title{_ISSUE_TITLE}")
    # Input and Result share one label column, so their values start at the same offset.
    value_left = (
        "views => views.map(view => view.querySelector('dd').getBoundingClientRect().left)"
    )
    input_left, result_left = fields.evaluate_all(value_left)
    assert abs(input_left - result_left) < 1
    # A markdown description reads as prose with working links, not as source.
    description = card.get_by_test_id("tool-prose-value")
    expect(description.get_by_role("link", name="#11607")).to_be_visible()
    expect(description).not_to_contain_text("](")
    font = description.locator("p").first.evaluate("el => getComputedStyle(el).fontFamily")
    assert "mono" not in font.lower()
    # A description's heading stays near body size instead of the chat's 1.4em.
    sizes = description.evaluate(
        "el => ['h2', 'p'].map("
        "tag => parseFloat(getComputedStyle(el.querySelector(tag)).fontSize))"
    )
    assert sizes[0] <= sizes[1] * 1.1, sizes
    font_size = "el => getComputedStyle(el).fontSize"
    label_size = fields.last.locator("dt").first.evaluate(font_size)
    raw = page.get_by_role("tab", name="Raw")
    expect(page.get_by_role("tab", name="Fields")).to_have_attribute("aria-selected", "true")
    raw.click()
    expect(raw).to_have_attribute("aria-selected", "true")
    expect(transcript).to_contain_text('"includeComments": false')
    # Raw names its panels like the Fields view's sections.
    expect(card.get_by_text("Input", exact=True)).to_be_visible()
    expect(card.get_by_text("Result", exact=True)).to_be_visible()
    expect(card).not_to_contain_text("Parameters")
    # Labels and code share one Interface font step, so the card reads as one size.
    code_sizes = card.locator("pre code").evaluate_all(
        "codes => codes.map(code => getComputedStyle(code).fontSize)"
    )
    assert set(code_sizes) == {label_size}, (label_size, code_sizes)

    issue.click()
    read = page.get_by_role("button", name=f"Read {_LONG_TOOL_PATH}", exact=True)
    edit = page.get_by_role("button", name=re.compile(r"^Edit ")).filter(
        has=page.get_by_test_id("tool-line-counts")
    )
    counts = edit.get_by_test_id("tool-line-counts")
    expect(counts).to_have_text("+2 -1")
    for row in (read, edit):
        link = row.get_by_role("link", name=_LONG_TOOL_PATH, exact=True)
        row.scroll_into_view_if_needed()
        geometry = link.evaluate("""link => {
            const path = link.getBoundingClientRect();
            const title = link.parentElement.getBoundingClientRect();
            const verb = link.parentElement.firstElementChild.getBoundingClientRect();
            const count = link.closest('button').querySelector('[data-testid="tool-line-counts"]');
            return {path: path.toJSON(), title: title.toJSON(), verb: verb.toJSON(),
                counts: count?.getBoundingClientRect().toJSON(),
                visiblePathWidth: Math.min(path.right, title.right) - path.left};
        }""")
        # The path must retain a readable prefix beside its verb, not disappear atomically.
        assert geometry["visiblePathWidth"] >= 44, geometry
        if app_kind:
            assert geometry["path"]["height"] >= 44, geometry
            assert geometry["path"]["width"] >= 44, geometry
            assert geometry["path"]["left"] >= geometry["verb"]["right"], geometry
            assert geometry["path"]["right"] <= geometry["title"]["right"] + 1, geometry
        if geometry["counts"]:
            assert geometry["counts"]["left"] >= geometry["title"]["right"], geometry
            assert geometry["counts"]["right"] <= width, geometry
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    file_content = "# File opened from a tool row"
    chat.contract.json(
        f"{environment_api}/filesystem/{_LONG_TOOL_PATH.lstrip('/')}",
        {
            "object": "session.environment.filesystem.file_content",
            "path": _LONG_TOOL_PATH,
            "content": file_content,
            "content_type": "text/markdown",
            "encoding": "utf-8",
            "bytes": len(file_content),
        },
    )
    link = edit.get_by_role("link", name=_LONG_TOOL_PATH, exact=True)
    if app_kind:
        link.tap()
    else:
        link.click()
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        "File opened from a tool row"
    )
    expect(edit).to_have_attribute("aria-expanded", "false")


_LIGATURE_COMMAND = "python3 -c \"s='a=>b'; assert s.count('=>')==1; print('===')\""
# JS evaluator returning the text nodes under root whose computed style still allows ligatures.
_LIGATED_TEXT = """root => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const ligated = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (!node.textContent.trim()) continue;
        const style = getComputedStyle(node.parentElement);
        const variant = style.fontVariantLigatures;
        const features = style.fontFeatureSettings.replace(/\\s+/g, "");
        const off = variant === "none"
            || (variant.includes("no-common-ligatures") && variant.includes("no-contextual"))
            || (features.includes('"liga"0') && features.includes('"calt"0'));
        if (!off) ligated.push(node.textContent);
    }
    return ligated;
}"""


def test_shell_command_renders_without_ligatures_like_chat_code(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A shell call's command keeps one glyph per character in its row and terminal panel."""
    chat = chat_session_contract
    response_id = "ligature-command"
    chronological = [
        message_item(
            "ligature-command-user", "user", "Check the anchor.", response_id=response_id
        ),
        *tool_call_items(
            "call-ligature-command",
            "Bash",
            {"command": _LIGATURE_COMMAND},
            "===\n",
            response_id=response_id,
        ),
        message_item(
            "ligature-command-answer",
            "assistant",
            "`s.count('=>')==1` holds.",
            response_id=response_id,
        ),
    ]
    chat.set_items(list(reversed(chronological)))
    page.goto(chat.url)

    inline_code = page.locator('[role="log"] [data-streamdown="inline-code"]')
    expect(inline_code).to_have_text("s.count('=>')==1", timeout=20_000)
    assert inline_code.evaluate(_LIGATED_TEXT) == []
    page.get_by_role("button", name=re.compile(r"^Worked")).click()
    page.get_by_text("Ran 1 shell command", exact=True).click()
    row = page.locator('[data-slot="collapsible-trigger"]', has_text="python3 -c")
    preview = row.get_by_text(_LIGATURE_COMMAND, exact=True)
    expect(preview).to_be_visible()
    row.click()
    terminal_command = page.get_by_test_id("shell-tool-terminal").locator("pre code").first
    expect(terminal_command).to_contain_text("assert s.count('=>')==1")

    ligated = {
        "row preview": preview.evaluate(_LIGATED_TEXT),
        "terminal command": terminal_command.evaluate(_LIGATED_TEXT),
    }
    assert ligated == {"row preview": [], "terminal command": []}, ligated


_PATCH_CHANGES = [
    {
        "path": "/repo/src/greet.py",
        "kind": {"type": "update"},
        "diff": (
            "@@ -1,3 +1,3 @@\n def greet(name):\n"
            "-    print('Hello ' + name)\n+    print(f'Hello {name}')\n     return name\n"
        ),
    },
    {
        "path": "/repo/src/farewell.py",
        "kind": {"type": "add"},
        "diff": "def farewell(name):\n    print(f'Bye {name}')\n",
    },
    {"path": "/repo/src/legacy.py", "kind": {"type": "delete"}, "diff": "LEGACY = True\n"},
]
_DECLINED_CHANGES = [
    {
        "path": "/repo/src/config.py",
        "kind": {"type": "update"},
        "diff": "@@ -1 +1 @@\n-DEBUG = False\n+DEBUG = True\n",
    }
]
# How the runner stores a Codex patch's output: its outcome, then one line per file.
_PATCH_OUTPUT = (
    "File change applied.\nupdate /repo/src/greet.py\nadd /repo/src/farewell.py\n"
    "delete /repo/src/legacy.py"
)
_DECLINED_OUTPUT = "Error: File change ended with status declined.\nupdate /repo/src/config.py"
_EDIT_ERROR = "<tool_use_error>String to replace not found in file.</tool_use_error>"


def _seed_edit_calls(chat: ChatSessionContract) -> None:
    """One settled turn: a Codex patch, a Claude write, a missed Claude edit, a declined patch."""
    response_id = "edit-rows-response"
    chronological = [
        message_item("edit-rows-user", "user", "Rename the greeting.", response_id=response_id),
        *tool_call_items(
            "call-patch",
            "apply_patch",
            {"changes": _PATCH_CHANGES},
            _PATCH_OUTPUT,
            response_id=response_id,
        ),
        *tool_call_items(
            "call-write",
            "Write",
            {"file_path": "/repo/src/hello.py", "content": "def hello():\n    return 'hi'\n"},
            "File created successfully at: /repo/src/hello.py",
            response_id=response_id,
        ),
        *tool_call_items(
            "call-edit",
            "Edit",
            {"file_path": "/repo/README.md", "old_string": "Hi", "new_string": "Hello"},
            _EDIT_ERROR,
            response_id=response_id,
        ),
        *tool_call_items(
            "call-declined",
            "apply_patch",
            {"changes": _DECLINED_CHANGES},
            _DECLINED_OUTPUT,
            response_id=response_id,
        ),
        message_item(
            "edit-rows-assistant", "assistant", "Greeting renamed.", response_id=response_id
        ),
    ]
    chat.set_items(list(reversed(chronological)))


def _open_edit_rows(page: Page, chat: ChatSessionContract) -> None:
    _seed_edit_calls(chat)
    page.goto(chat.url)
    expect(page.get_by_text("Greeting renamed.")).to_be_visible(timeout=20_000)
    page.get_by_role("button", name=re.compile(r"^Worked")).click()
    page.get_by_text(re.compile(r"^Edited \d files$")).click()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
def test_patch_row_counts_lines_across_its_files(
    page: Page,
    chat_session_contract: ChatSessionContract,
    width: int,
) -> None:
    """Patch and new-file write rows show their line counts and expand to per-file diffs."""
    page.set_viewport_size({"width": width, "height": 720})
    _open_edit_rows(page, chat_session_contract)

    patch = page.get_by_role("button", name=re.compile(r"^Edit 3 files"))
    expect(patch.get_by_test_id("tool-line-counts")).to_have_text("+3 -2")

    patch.click()
    expect(page.get_by_role("tab", name="Diff")).to_have_attribute("aria-selected", "true")
    # Each file is its own panel, named with its change kind and counts.
    body = patch.locator("xpath=..")
    diffs = body.locator('[data-language="diff"]')
    expect(diffs).to_have_count(3)
    expect(body.get_by_text("/repo/src/farewell.py (new file)")).to_be_visible()
    expect(body.get_by_text("/repo/src/legacy.py (deleted)")).to_be_visible()
    expect(diffs.nth(1)).to_contain_text("+    print(f'Bye {name}')")
    expect(diffs.nth(2)).to_contain_text("-LEGACY = True")
    expect(body).not_to_contain_text("--- /repo/src")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    # A write that created its file reads like an edit: counted, and all added lines.
    write = page.get_by_role("button", name=re.compile(r"^Write .*hello\.py"))
    expect(write.get_by_test_id("tool-line-counts")).to_have_text("+2")
    write.click()
    written = write.locator("xpath=..")
    expect(written.get_by_text("/repo/src/hello.py (new file)")).to_be_visible()
    expect(written.locator('[data-language="diff"]')).to_contain_text("+    return 'hi'")


@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
def test_failed_edit_rows_show_their_error(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Missed and declined edits show no counts, and their errors only after expansion."""
    _open_edit_rows(page, chat_session_contract)

    missed = page.get_by_role("button", name=re.compile(r"^Edit .*README\.md"))
    missed_error = page.get_by_text("String to replace not found in file.", exact=True)
    expect(missed_error).not_to_be_visible()
    missed.click()
    expect(missed.locator("xpath=..").get_by_test_id("tool-error-box")).to_have_text(
        "String to replace not found in file."
    )
    missed.click()
    expect(missed_error).not_to_be_visible()
    declined = page.get_by_role("button", name=re.compile(r"^Edit .*config\.py"))
    expect(
        page.get_by_text("Error: File change ended with status declined.", exact=True)
    ).not_to_be_visible()
    for row in (missed, declined):
        expect(row.get_by_test_id("tool-line-counts")).to_have_count(0)

    declined.click()
    body = declined.locator("xpath=..")
    expect(body.get_by_test_id("tool-error-box")).to_contain_text(
        "File change ended with status declined."
    )
    expect(body.get_by_text("Requested change, not applied:")).to_be_visible()
    expect(body.locator('[data-language="diff"]')).to_contain_text("+DEBUG = True")
    declined.click()
    expect(body.get_by_test_id("tool-error-box")).not_to_be_visible()


def test_automation_context_is_hidden_from_transcript(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    from omnigent.server.scheduled.fire import UNATTENDED_RUN_NOTE

    chat = chat_session_contract
    chat.set_items(
        [
            message_item(
                "automation-task",
                "user",
                f"Summarize **today's changes**.\n\n{UNATTENDED_RUN_NOTE}",
                response_id="automation-turn",
            )
        ]
    )
    page.goto(chat.url)
    user = page.locator('[data-role="user"]')
    expect(user).to_contain_text("Summarize today's changes.")
    expect(user).not_to_contain_text("no one is watching")
    expect(user).not_to_contain_text("omnigent-automation-context")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_view_image_tool_opens_image(
    page: Page,
    chat_session_contract: ChatSessionContract,
    width: int,
) -> None:
    """A persisted Codex imageView call opens the existing file image preview."""
    chat = chat_session_contract
    chat.harness = "codex-native"
    image_path = "/browser-workspace/screenshot.png"
    chat.set_items(
        list(
            reversed(
                tool_call_items(
                    "image-view",
                    "view_image",
                    {"path": image_path},
                    image_path,
                    response_id="image-view-response",
                )
            )
        )
    )
    environment_api = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    chat.contract.json(environment_api, {"metadata": {"root": "/browser-workspace"}})
    for resource in ("changes", "filesystem"):
        chat.contract.json(f"{environment_api}/{resource}", list_payload([]))
    chat.contract.json(f"/v1/sessions/{chat.session_id}/comments", [])
    image = (Path(__file__).resolve().parents[2] / "resources/test_image.png").read_bytes()
    chat.contract.json(
        f"{environment_api}/filesystem/screenshot.png",
        {
            "object": "session.environment.filesystem.file_content",
            "path": "screenshot.png",
            "content": base64.b64encode(image).decode(),
            "content_type": "image/png",
            "encoding": "base64",
            "bytes": len(image),
        },
    )
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(chat.url)
    page.get_by_role("button", name="Called 1 tool", exact=True).click()
    row = page.get_by_role("button", name=re.compile(r"^View image "))
    expect(row).to_be_visible()
    row.get_by_role("link", name="screenshot.png", exact=True).click()
    preview = page.locator('[data-testid="file-viewer"]:visible').get_by_role("img")
    expect(preview).to_be_visible()
    expect(preview).to_have_js_property("naturalWidth", 100)
    expect(row).to_have_attribute("aria-expanded", "false")
