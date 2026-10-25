"""Real-browser coverage for transcript rendering that depends on layout or chunks."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.browser_ui.chat._geometry_helpers import open_live, settled_geometry
from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

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
    """The built SPA serves Mermaid's lazy chunk and renders a real diagram."""
    chat = chat_session_contract
    _seed_assistant_message(
        chat,
        "```mermaid\nflowchart LR\n  A[Client] --> B[Server]\n```",
    )
    page.goto(chat.url)

    block = page.locator('[data-streamdown="mermaid-block"]').first
    expect(block).to_be_visible(timeout=20_000)
    expect(block.locator("svg[aria-roledescription]")).to_be_visible(timeout=20_000)


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
