"""E2E: wide native shells — scrolled transcript text must fade out before the chat header.

The chat header paints no background; the conversation viewport's
``chat-scroll-fade`` mask dissolves text before it slides under the header
controls. Native shells push the header down by the OS top inset at every
width, so the fade has to move with it. The journey opens a long
conversation in an emulated Android or iOS shell at 768 CSS px or wider with
a top inset, checks that the first message starts where the mask is fully
opaque (a shifted fade must not swallow the top of the conversation), then
scrolls partway up until a line of text sits behind the header controls and
checks that the mask is fully transparent down to the bottom of the lowest
header glyph.

The shells are emulated the way the sibling mobile tests do: a stub
``window.omnigentNative`` tags the app root ``data-android-native`` /
``data-ios-native``. Android additionally runs the real ``ensureInsetStyles``
block from ``NativeBridgeScript.kt`` and receives the inset as the inline root
vars ``MainActivity.emitInsets`` writes; iOS receives it through CDP so
``env(safe-area-inset-top)`` resolves to the inset.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

_FADE = '[role="log"].chat-scroll-fade'
_HEADER = "header.chat-header"

_FOLD_VIEWPORT = {"width": 848, "height": 706}
_EDGE_VIEWPORT = {"width": 768, "height": 706}
_NARROW_VIEWPORT = {"width": 767, "height": 706}
_IPAD_VIEWPORT = {"width": 1024, "height": 768}

_ANDROID_BRIDGE_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "web/android/app/src/main/java/ai/omnigent/android/NativeBridgeScript.kt"
)

_ANDROID_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
};
"""

_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onViewModeChanged: function () { return function () {}; },
  setViewMode: function () {},
  setServerSwitcherHidden: function () {},
  setSidebarOpen: function () {},
};
"""

_PROSE = (
    "The build pipeline reads the lockfile, resolves every workspace package, "
    "compiles the SPA, and only then starts the server, so a cold start keeps the "
    "machine busy for a while before the first page renders."
)

_TURNS = 18

# The conversation viewport is the tallest scrollable descendant of the log.
_FIND_SCROLLER = """
  const log = document.querySelector('[role="log"]');
  let best = null;
  log.querySelectorAll('*').forEach((el) => {
    const taller = !best || el.scrollHeight > best.scrollHeight;
    if (el.scrollHeight > el.clientHeight + 4 && taller) best = el;
  });
"""
_SCROLLER_TOP = f"() => {{ {_FIND_SCROLLER} return best ? best.scrollTop : -1; }}"
_SET_SCROLL = (
    f"(target) => {{ {_FIND_SCROLLER} "
    "if (best) best.scrollTop = target === 'top' ? 0 : best.scrollHeight; }"
)
_NUDGE_SCROLL = f"(delta) => {{ {_FIND_SCROLLER} if (best) best.scrollTop += delta; }}"

_FADE_GEOMETRY = """
() => {
  const log = document.querySelector('[role="log"].chat-scroll-fade');
  const style = getComputedStyle(log);
  const mask = style.maskImage !== "none" ? style.maskImage : style.webkitMaskImage;
  const stops = [...mask.matchAll(/(\\d+(?:\\.\\d+)?)px/g)].map((m) => parseFloat(m[1]));
  const box = log.getBoundingClientRect();
  return {
    mask,
    transparentUntil: box.top + stops[0],
    opaqueFrom: box.top + stops[1],
  };
}
"""

_HEADER_GEOMETRY = """
() => {
  const header = document.querySelector("header.chat-header");
  const rect = (el) => el.getBoundingClientRect();
  const visible = (el) => rect(el).width > 0 && rect(el).height > 0;
  const buttons = [...header.querySelectorAll("button")].filter(visible);
  const glyphs = buttons.flatMap((b) => [...b.querySelectorAll("svg, span")].filter(visible));
  const bottom = (els) => Math.max(...els.map((el) => rect(el).bottom));
  const top = (els) => Math.min(...els.map((el) => rect(el).top));
  return {
    headerTop: rect(header).top,
    headerBottom: rect(header).bottom,
    controlsTop: top(buttons),
    controlsBottom: bottom(buttons),
    glyphBottom: bottom(glyphs),
    labels: buttons.map((b) => b.getAttribute("aria-label") || b.textContent.trim()),
    panelOpen: !!header.querySelector('button[aria-label="Collapse right panel"]'),
  };
}
"""

_TEXT_LINE_IN_BAND = """
(band) => {
  const log = document.querySelector('[role="log"]');
  const walker = document.createTreeWalker(log, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node;
  while ((node = walker.nextNode())) {
    if (!node.textContent.trim()) continue;
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (r.width < band.minWidth) continue;
      const overlap = Math.min(r.bottom, band.bottom) - Math.max(r.top, band.top);
      if (overlap >= band.minOverlap) return true;
    }
  }
  return false;
}
"""

_FIRST_BUBBLE_TOP = """
() => {
  const first = document.querySelector('[data-testid="message-bubble"]');
  return first ? first.getBoundingClientRect().top : null;
}
"""


def _android_inset_styles_script() -> str:
    """Return the ``ensureInsetStyles`` block the Android shell injects at document start."""
    source = _ANDROID_BRIDGE_SOURCE.read_text()
    start = source.index("const ensureInsetStyles = () => {")
    end_marker = (
        'else document.addEventListener("DOMContentLoaded", ensureInsetStyles, { once: true });'
    )
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end]


def _seed_message(
    client: httpx.Client, session_id: str, *, response_id: str, role: str, text: str
) -> None:
    content_type = "input_text" if role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }
    if role == "assistant":
        item_data["agent"] = "e2e-history"
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {"item_type": "message", "item_data": item_data, "response_id": response_id},
        },
    )
    assert response.status_code == 202, response.text


def _seed_long_transcript(base_url: str, session_id: str) -> str:
    """Seed enough committed turns to overflow the viewport; return the newest reply's prefix."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        for index in range(1, _TURNS + 1):
            _seed_message(
                client,
                session_id,
                response_id=f"resp_{index:03d}",
                role="user",
                text=f"Question {index}: what happens while I scroll a long conversation?",
            )
            _seed_message(
                client,
                session_id,
                response_id=f"resp_{index:03d}",
                role="assistant",
                text=f"Answer {index}. {_PROSE}",
            )
    return f"Answer {_TURNS}."


def _open_shell(
    browser: Browser, shell: str, viewport: dict[str, int], inset_top: int, panel: str
) -> tuple[BrowserContext, Page]:
    context = browser.new_context(
        viewport=viewport, is_mobile=shell != "web", has_touch=shell != "web"
    )
    page = context.new_page()
    if panel == "open":
        page.add_init_script(
            "window.localStorage.setItem('omnigent:default-workspace-panel', 'open')"
        )
    if shell == "android":
        page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
        page.add_init_script(_android_inset_styles_script())
    elif shell == "ios":
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
        cdp = context.new_cdp_session(page)
        cdp.send(
            "Emulation.setSafeAreaInsetsOverride",
            {"insets": {"top": inset_top, "left": 0, "bottom": 0, "right": 0}},
        )
    return context, page


def _apply_android_inset(page: Page, inset_top: int) -> None:
    page.evaluate(
        """(top) => {
          const style = document.documentElement.style;
          style.setProperty("--omnigent-safe-top", top);
          style.setProperty("--omnigent-android-safe-area-top", top);
        }""",
        f"{inset_top}px",
    )


def _scroll_until_text_sits_under_header(page: Page, controls: dict[str, float]) -> None:
    """Wheel partway up, then settle with a line of message text behind the header controls."""
    log = page.locator(_FADE)
    box = log.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(6):
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(120)
    previous = -2
    for _ in range(20):
        current = page.evaluate(_SCROLLER_TOP)
        if current == previous:
            break
        previous = current
        page.wait_for_timeout(100)
    assert previous > 0, f"transcript did not scroll (scrollTop={previous})"
    # A prose-width line, so a short label never counts as text behind the controls.
    band = {
        "top": controls["controlsTop"],
        "bottom": controls["controlsBottom"],
        "minOverlap": 12,
        "minWidth": 160,
    }
    for _ in range(40):
        if page.evaluate(_TEXT_LINE_IN_BAND, band):
            return
        page.evaluate(_NUDGE_SCROLL, -6)
        page.wait_for_timeout(50)
    raise AssertionError("could not place a line of message text behind the header controls")


def _beat(page: Page, ms: int) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(ms)


def _save_screenshot(page: Page, name: str) -> None:
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if not record_dir:
        return
    shots = Path(record_dir).parent / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots / f"{name}.png"))


def _describe(header: dict[str, float], fade: dict[str, float]) -> str:
    return (
        f"header {header['headerTop']:.0f}-{header['headerBottom']:.0f}, controls "
        f"{header['controlsTop']:.0f}-{header['controlsBottom']:.0f}, glyph bottom "
        f"{header['glyphBottom']:.0f}, fade transparent until {fade['transparentUntil']:.0f}, "
        f"opaque from {fade['opaqueFrom']:.0f}"
    )


@pytest.mark.parametrize(
    ("shell", "viewport", "inset_top", "panel"),
    [
        pytest.param("android", _FOLD_VIEWPORT, 48, "collapsed", id="android-848-48"),
        pytest.param("android", _FOLD_VIEWPORT, 48, "open", id="android-848-48-panel-open"),
        pytest.param("android", _FOLD_VIEWPORT, 32, "collapsed", id="android-848-32"),
        pytest.param("android", _EDGE_VIEWPORT, 48, "collapsed", id="android-768-48"),
        pytest.param("ios", _IPAD_VIEWPORT, 48, "collapsed", id="ios-1024-48"),
        pytest.param("ios", _IPAD_VIEWPORT, 60, "collapsed", id="ios-1024-60"),
        pytest.param("android", _NARROW_VIEWPORT, 48, "collapsed", id="android-767-48"),
        pytest.param("android", _FOLD_VIEWPORT, 0, "collapsed", id="android-848-0"),
        pytest.param("web", _FOLD_VIEWPORT, 0, "collapsed", id="web-848-0"),
    ],
)
def test_scrolled_transcript_fades_before_the_inset_header(
    browser: Browser,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
    shell: str,
    viewport: dict[str, int],
    inset_top: int,
    panel: str,
) -> None:
    """The first message clears the fade and scrolled text is faded under the header glyphs."""
    base_url, session_id = seeded_session
    newest_reply = _seed_long_transcript(base_url, session_id)
    case = request.node.callspec.id

    context, page = _open_shell(browser, shell, viewport, inset_top, panel)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        app_shell = page.locator(".app-shell")
        if shell == "web":
            expect(app_shell).to_be_visible()
            assert app_shell.get_attribute("data-android-native") is None
            assert app_shell.get_attribute("data-ios-native") is None
        else:
            expect(app_shell).to_have_attribute(f"data-{shell}-native", "true")
        if shell == "android":
            _apply_android_inset(page, inset_top)

        expect(page.get_by_text(newest_reply, exact=False).first).to_be_visible(timeout=20_000)
        expect(page.locator(_HEADER)).to_be_visible()
        header = page.evaluate(_HEADER_GEOMETRY)
        # The mask does not depend on the scroll position, so measure it once.
        fade = page.evaluate(_FADE_GEOMETRY)
        page.evaluate(_SET_SCROLL, "top")
        page.wait_for_timeout(300)
        first_bubble_top = page.evaluate(_FIRST_BUBBLE_TOP)
        print(
            f"[{case}] at scrollTop 0: first bubble top={first_bubble_top}, header "
            f"{header['headerTop']:.0f}-{header['headerBottom']:.0f}, "
            f"panel_open={header['panelOpen']}, controls={header['labels']}"
        )
        assert first_bubble_top is not None and first_bubble_top >= fade["opaqueFrom"] - 1.0, (
            f"{shell} shell at {viewport['width']}px with a {inset_top}px top inset: at scroll "
            f"position 0 the first message starts at y={first_bubble_top}, inside the fade "
            f"({_describe(header, fade)})"
        )
        page.evaluate(_SET_SCROLL, "bottom")
        page.wait_for_timeout(300)
        _beat(page, 1_200)

        _scroll_until_text_sits_under_header(page, header)
        _beat(page, 2_500)
        _save_screenshot(page, case)
        ramp = max(fade["opaqueFrom"] - fade["transparentUntil"], 1.0)
        visible = min(1.0, max(0.0, (header["glyphBottom"] - fade["transparentUntil"]) / ramp))
        print(f"[{case}] {_describe(header, fade)}, mask={fade['mask']}")
        assert fade["transparentUntil"] >= header["glyphBottom"] - 1.0, (
            f"{shell} shell at {viewport['width']}px with a {inset_top}px top inset: scrolled "
            f"text behind the header controls is up to {visible:.0%} visible "
            f"({_describe(header, fade)})"
        )
    finally:
        context.close()
