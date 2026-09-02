"""E2E: the Termux-style extra-keys row under the terminal on touch devices.

Soft keyboards cannot send Esc, Tab, Shift+Tab, Ctrl-x, Alt-x or the arrow
keys, so touch devices get a fixed 2×7 row of those keys under the xterm
mount (``TerminalExtraKeys`` in ``web/src/components/blocks``). Visibility is
touch-based, never width-based: a ``has_touch`` context flips
``(pointer: coarse)`` and shows the row at a phone *and* a tablet width, while
the default fine-pointer context never renders it.

The bytes a tap produces are asserted from a file the shell writes (a raw-mode
``cat``), not from the screen: xterm renders to a WebGL canvas, so its output
never reaches the DOM.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_PHONE = {"width": 390, "height": 844}
_TABLET = {"width": 1024, "height": 1366}
_ROW = "terminal-extra-keys"
_COARSE = "matchMedia('(pointer: coarse)').matches"
_NARROW = "matchMedia('(max-width: 767.98px)').matches"


def _touch_context(browser: Browser, viewport: dict[str, int]) -> BrowserContext:
    """A touch-primary context (``has_touch`` flips ``pointer: coarse``)."""
    return browser.new_context(
        viewport=viewport,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )


def _ensure_coarse_pointer(page: Page) -> None:
    """Assert the capability media the row keys on, with a CDP fallback.

    Chromium reports ``pointer: coarse`` under touch emulation; if a renderer
    ever stops doing so, force the feature through CDP so the test still pins
    the row's behaviour rather than the emulation's.
    """
    if page.evaluate(_COARSE):
        return
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Emulation.setEmulatedMedia",
        {"features": [{"name": "pointer", "value": "coarse"}]},
    )
    assert page.evaluate(_COARSE), "touch context must report a coarse primary pointer"


def _open_shell(page: Page, base_url: str, session_id: str):
    """Open a fresh user shell and return its connected ``terminal-view``.

    Narrow layouts create shells from the header kebab's Shells drawer (the
    new shell then takes over the main column); wide layouts (the tablet, the
    desktop default) use the workspace rail's "+" menu and render the shell in
    the rail. Both end in a connected xterm, which is all the row needs.
    """
    page.goto(f"{base_url}/c/{session_id}")
    if page.evaluate(_NARROW):
        page.get_by_role("button", name="Conversation actions").click()
        shells_entry = page.get_by_role("menuitem", name="Shells", exact=True)
        expect(shells_entry).to_be_visible(timeout=10_000)
        shells_entry.click()
        drawer = page.get_by_test_id("shells-panel-drawer")
        expect(drawer).to_have_attribute("data-state", "open")
        drawer.get_by_role("button", name="New shell").click()
        # A user shell takes over the main column on narrow layouts.
        terminal_view = (
            page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
            .get_by_test_id("terminal-view")
            .last
        )
    else:
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("button", name="Open new").click()
        page.get_by_role("menuitem", name=re.compile("Shell")).click()
        terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def _row_metrics(page: Page, row) -> dict[str, float]:
    """Grid geometry the layout contract pins: columns, overflow, heights."""
    return row.evaluate(
        """(el) => {
          const rect = el.getBoundingClientRect();
          const columns = getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length;
          const keys = [...el.querySelectorAll('button')].map((b) => b.getBoundingClientRect());
          return {
            height: rect.height,
            width: rect.width,
            columns,
            overflows: el.scrollWidth > el.clientWidth + 1,
            keyCount: keys.length,
            minKeyHeight: Math.min(...keys.map((k) => k.height)),
            rows: new Set(keys.map((k) => Math.round(k.top))).size,
          };
        }"""
    )


def test_extra_keys_row_shows_on_touch_at_phone_and_tablet_widths(
    browser: Browser, terminal_session: tuple[str, str]
) -> None:
    """Touch shows the same fixed 2×7 row at 390px and at 1024px.

    The tablet width is the width-independence proof: nothing in the row keys
    on a viewport query, so an iPad-sized touch surface gets the row too, with
    wider columns, identical height, no wrapping and no horizontal scroll.
    """
    base_url, session_id = terminal_session
    heights: dict[str, float] = {}
    for label, viewport in (("phone", _PHONE), ("tablet", _TABLET)):
        context = _touch_context(browser, viewport)
        page = context.new_page()
        try:
            terminal_view = _open_shell(page, base_url, session_id)
            _ensure_coarse_pointer(page)

            row = terminal_view.get_by_test_id(_ROW)
            expect(row).to_be_visible()
            expect(row.get_by_role("button")).to_have_count(14)
            metrics = _row_metrics(page, row)
            assert metrics["columns"] == 7, metrics
            assert metrics["rows"] == 2, metrics
            assert metrics["keyCount"] == 14, metrics
            assert not metrics["overflows"], metrics
            assert metrics["minKeyHeight"] >= 44, metrics
            # The row spans its terminal surface edge to edge.
            surface = terminal_view.bounding_box()
            assert surface is not None
            assert abs(metrics["width"] - surface["width"]) <= 2, (metrics, surface)
            heights[label] = metrics["height"]
        finally:
            context.close()

    assert abs(heights["phone"] - heights["tablet"]) < 1, heights


def test_extra_keys_row_absent_on_fine_pointer(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """The default desktop context never renders the row (not even hidden)."""
    base_url, session_id = terminal_session
    terminal_view = _open_shell(page, base_url, session_id)
    assert not page.evaluate(_COARSE)
    expect(terminal_view.get_by_test_id(_ROW)).to_have_count(0)


def _wait_for_bytes(log: Path, expected: bytes, timeout_s: float = 15) -> bytes:
    deadline = time.monotonic() + timeout_s
    data = b""
    while time.monotonic() < deadline:
        data = log.read_bytes() if log.exists() else b""
        if len(data) >= len(expected):
            break
        time.sleep(0.25)
    return data


def test_extra_keys_send_the_expected_bytes(
    browser: Browser, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """⇧Tab, Esc, Ctrl+c and Alt+p reach the shell as the right bytes.

    A raw-mode ``cat`` records stdin to an absolute ``tmp_path`` file (the
    shell and this test share a host). Expected stream: ``\\x1b[Z`` (legacy
    CSI Z, never CSI-u), a lone ``\\x1b``, ``\\x03`` (armed Ctrl rewrote the
    typed ``c``) and ``\\x1bp`` (armed Alt prefixed the typed ``p``). The
    modifiers disarm after one use.
    """
    base_url, session_id = terminal_session
    log = tmp_path / "keys.txt"
    context = _touch_context(browser, _PHONE)
    page = context.new_page()
    try:
        terminal_view = _open_shell(page, base_url, session_id)
        _ensure_coarse_pointer(page)
        row = terminal_view.get_by_test_id(_ROW)
        expect(row).to_be_visible()

        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.keyboard.type(f"stty raw -echo; cat > '{log}'")
        page.keyboard.press("Enter")
        # Let the mode change round-trip before the first tap.
        page.wait_for_timeout(1_500)

        row.get_by_role("button", name="Shift Tab").tap()
        row.get_by_role("button", name="Escape").tap()

        ctrl = row.get_by_role("button", name="Control")
        ctrl.tap()
        expect(ctrl).to_have_attribute("aria-pressed", "true")
        page.keyboard.type("c")
        expect(ctrl).to_have_attribute("aria-pressed", "false")

        alt = row.get_by_role("button", name="Alt")
        alt.tap()
        expect(alt).to_have_attribute("aria-pressed", "true")
        page.keyboard.type("p")
        expect(alt).to_have_attribute("aria-pressed", "false")

        expected = b"\x1b[Z\x1b\x03\x1bp"
        data = _wait_for_bytes(log, expected)
        assert data == expected, f"shell received {data!r}, expected {expected!r}"
    finally:
        context.close()


def test_extra_keys_keep_focus_and_scrollback(
    browser: Browser, terminal_session: tuple[str, str]
) -> None:
    """Plain keys never move focus, and the terminal still scrolls with the row.

    Esc must not summon or dismiss the soft keyboard: when xterm's textarea
    is focused a tap leaves it focused, and when nothing is focused a tap
    leaves focus alone. The row sits outside the xterm element, so the
    scrollback still scrolls beneath it.
    """
    base_url, session_id = terminal_session
    context = _touch_context(browser, _PHONE)
    page = context.new_page()
    try:
        terminal_view = _open_shell(page, base_url, session_id)
        _ensure_coarse_pointer(page)
        row = terminal_view.get_by_test_id(_ROW)
        expect(row).to_be_visible()
        esc = row.get_by_role("button", name="Escape")

        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.keyboard.type("seq 1 400")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1_500)

        is_textarea_active = "document.activeElement?.classList.contains('xterm-helper-textarea')"
        assert page.evaluate(is_textarea_active)
        esc.tap()
        assert page.evaluate(is_textarea_active), "Esc tap must keep xterm's textarea focused"

        page.evaluate("document.activeElement?.blur()")
        assert not page.evaluate(is_textarea_active)
        esc.tap()
        assert not page.evaluate(is_textarea_active), "a plain key must not focus the terminal"

        # xterm 6 scrolls through a custom scrollable element (no DOM
        # scrollTop); its vertical slider's offset tracks the scroll position.
        slider = terminal_view.locator(".xterm-scrollable-element .scrollbar.vertical .slider")
        expect(slider).to_have_count(1)
        slider_top = "el => parseFloat(el.style.top) || 0"
        before = slider.evaluate(slider_top)
        assert before > 0, "seq output should have produced scrollback"
        screen = terminal_view.locator(".xterm-screen").first
        box = screen.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, -300)
        deadline = time.monotonic() + 10
        after = before
        while time.monotonic() < deadline:
            after = slider.evaluate(slider_top)
            if after < before:
                break
            page.wait_for_timeout(200)
        assert after < before, f"scrollback did not scroll (slider {before} -> {after})"
        expect(row).to_be_visible()
    finally:
        context.close()


_NATIVE_SHELL_PROBE = """
() => {
  const shell = document.querySelector('.app-shell');
  shell.setAttribute('data-ios-native', 'true');
  const probe = document.createElement('div');
  probe.className = 'main-terminal-view';
  shell.appendChild(probe);
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize);
  const shellStyle = getComputedStyle(shell);
  const result = {
    overflow: shellStyle.overflow,
    height: parseFloat(shellStyle.height),
    innerHeight: window.innerHeight,
    paddingTop: parseFloat(getComputedStyle(probe).paddingTop),
    expectedPaddingTop: 3.25 * root,
  };
  probe.remove();
  return result;
}
"""


def test_native_shell_terminal_insets_apply_at_phone_and_tablet_widths(
    browser: Browser, seeded_session: tuple[str, str]
) -> None:
    """The iOS-shell viewport lock and terminal inset hold at every width.

    The native-shell rules used to live under ``@media (width < 48rem)``; an
    iPad-width shell got neither the viewport lock nor the terminal top inset.
    Marking the shell ``data-ios-native`` and probing a ``.main-terminal-view``
    element must yield the same computed values at 390px (unchanged from before)
    and at 1024px (newly covered): ``overflow: hidden``, a height locked to the
    viewport, and ``padding-top`` of ``3.25rem`` plus the (zero) OS inset.
    """
    base_url, session_id = seeded_session
    for viewport in (_PHONE, _TABLET):
        context = _touch_context(browser, viewport)
        page = context.new_page()
        try:
            page.goto(f"{base_url}/c/{session_id}")
            expect(page.locator(".app-shell")).to_be_visible(timeout=30_000)
            probe = page.evaluate(_NATIVE_SHELL_PROBE)
            assert probe["overflow"] == "hidden", (viewport, probe)
            assert abs(probe["height"] - probe["innerHeight"]) <= 1, (viewport, probe)
            assert abs(probe["paddingTop"] - probe["expectedPaddingTop"]) <= 0.5, (viewport, probe)
        finally:
            context.close()
