"""E2E: an IME auto-pair on a phone must not corrupt the next composed candidate.

Journey (phone layout): open a shell from the header kebab → the touch keyboard
auto-inserts ``()`` and leaves its caret between the pair → compose ``ni`` and
select 你 → the terminal should receive ``()`` plus one cursor-left, then 你.

CI has no physical IME, so the keyboard's event sequence is replayed at xterm's
helper textarea. xterm's listeners do not check ``isTrusted``, so its
CompositionHelper runs exactly as with a real IME. The contract is read off the
attach WebSocket: the bytes the PTY receives. That byte contract is
program-independent — the shell program only decides whether it then echoes a
clean line, so the PTY echo is captured for context, not asserted on.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable

from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.shells.test_terminal_ime_composition import _capture_attach_frames

# iPhone-class portrait viewport: below the ``md`` breakpoint, so the header
# kebab (the phone user's shell entry point) renders.
_VIEWPORT = {"width": 390, "height": 844}

CANDIDATE = "你"
# Normal and DECCKM (application cursor keys) encodings of one cursor-left.
CURSOR_LEFT = (b"\x1b[D", b"\x1bOD")

_ANSI = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1bO[@-~]|\x1b[()][0-~]|\x1b[=>78]"
    rb"|\x1b\].*?(?:\x07|\x1b\\)|\r|\x07"
)

# The touch keyboard's auto-pair: an IME "Process" keydown (keyCode 229), the
# pair landing in the textarea with the caret between the parentheses, the
# input event, then keyup.
_AUTO_PAIR = """(ta) => {
  const key = (type) => {
    const ev = new KeyboardEvent(type, {
      key: "Process", keyCode: 229, bubbles: true, cancelable: true, composed: true,
    });
    if (ev.keyCode !== 229) Object.defineProperty(ev, "keyCode", { value: 229 });
    ta.dispatchEvent(ev);
  };
  key("keydown");
  ta.value = "()";
  ta.setSelectionRange(1, 1);
  ta.dispatchEvent(new InputEvent("input", {
    inputType: "insertText", data: "()", bubbles: true, composed: true,
  }));
  key("keyup");
  return { value: ta.value, caret: ta.selectionStart };
}"""

# One IME composition step: optional compositionstart, the textarea's new
# value/caret, compositionupdate, optional compositionend, then the input event.
_COMPOSITION_STEP = """(ta, step) => {
  const comp = (type, data) =>
    ta.dispatchEvent(new CompositionEvent(type, { data, bubbles: true, composed: true }));
  if (step.start) comp("compositionstart", "");
  ta.value = step.value;
  ta.setSelectionRange(step.caret, step.caret);
  comp("compositionupdate", step.data);
  if (step.end) comp("compositionend", step.data);
  ta.dispatchEvent(new InputEvent("input", {
    inputType: "insertCompositionText", data: step.data, bubbles: true, composed: true,
  }));
  return { value: ta.value, caret: ta.selectionStart };
}"""


def _open_mobile_shell(page: Page) -> Locator:
    """Header kebab → Shells → New shell; return the connected terminal view."""
    kebab = page.get_by_test_id("header-conversation-actions").or_(
        page.get_by_test_id("session-actions-menu")
    )
    expect(kebab).to_be_visible(timeout=60_000)
    kebab.click()
    page.get_by_role("menuitem", name="Shells").click()
    drawer = page.get_by_test_id("shells-panel-drawer")
    expect(drawer).to_be_visible(timeout=10_000)
    drawer.get_by_role("button", name="New shell").click()
    # The newest terminal-view is the shell just created; an earlier one may
    # belong to the agent, so target .last like the sibling composition test.
    connected = page.get_by_test_id("terminal-view").last
    expect(connected).to_be_visible(timeout=60_000)
    expect(connected).to_have_attribute("data-state", "connected", timeout=20_000)
    return connected


def _wait_for_frames(
    page: Page,
    frames: list[bytes],
    baseline: int,
    predicate: Callable[[bytes], bool],
    timeout_s: float,
) -> bytes:
    """Poll the frames appended after *baseline* until *predicate* holds or time runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        joined = b"".join(frames[baseline:])
        if predicate(joined):
            return joined
        page.wait_for_timeout(100)
    return b"".join(frames[baseline:])


def _echo_text(frames: list[bytes], baseline: int) -> bytes:
    return _ANSI.sub(b"", b"".join(frames[baseline:]))


def _settle(page: Page, frames: list[bytes], quiet_s: float, timeout_s: float) -> None:
    """Wait until no new frames have arrived for *quiet_s* (prompt painted)."""
    deadline = time.monotonic() + timeout_s
    last, stable_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        cur = len(b"".join(frames))
        if cur != last:
            last, stable_since = cur, time.monotonic()
        elif time.monotonic() - stable_since > quiet_s:
            return
        page.wait_for_timeout(150)


def test_ime_autopair_then_composition_lands_inside_pair(
    browser: Browser, terminal_session: tuple[str, str]
) -> None:
    """Composing a candidate inside an auto-inserted pair must reach the PTY as ``(你)``.

    Expected: the PTY receives ``()`` plus one cursor-left, then 你. The bug
    this catches: the pair reaches the PTY with no cursor-left (the caret stays
    after ``)``) and the composition commits ``)`` instead of 你, so the PTY
    sees ``())``.
    """
    base_url, session_id = terminal_session

    ctx_kwargs: dict = {"viewport": _VIEWPORT, "has_touch": True, "is_mobile": True}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir

    context = browser.new_context(**ctx_kwargs)
    try:
        page = context.new_page()
        sent, received = _capture_attach_frames(page)
        page.goto(f"{base_url}/c/{session_id}")
        terminal_view = _open_mobile_shell(page)

        prompt = _wait_for_frames(page, received, 0, lambda b: len(b) > 0, timeout_s=30)
        assert prompt, "no PTY output arrived after the terminal reported connected"
        # Let the prompt finish painting so the recorded surface is settled.
        _settle(page, received, quiet_s=1.5, timeout_s=10)

        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()

        # A first-run shell may open on a theme picker that consumes keystrokes
        # as navigation; dismiss it so input lands on a prompt that echoes.
        if b"Choose your theme" in b"".join(received):
            textarea.press("Enter")
            _settle(page, received, quiet_s=1.5, timeout_s=10)

        echo_baseline = len(received)

        pair_baseline = len(sent)
        state = textarea.evaluate(_AUTO_PAIR)
        assert state == {"value": "()", "caret": 1}, state
        pair_frames = _wait_for_frames(
            page, sent, pair_baseline, lambda b: b"()" in b, timeout_s=5
        )
        assert b"()" in pair_frames, f"the auto-pair never reached the PTY; sent: {pair_frames!r}"
        pair_frames = _wait_for_frames(
            page,
            sent,
            pair_baseline,
            lambda b: any(cl in b for cl in CURSOR_LEFT),
            timeout_s=2,
        )
        cursor_left_sent = any(cl in pair_frames for cl in CURSOR_LEFT)

        textarea.evaluate(
            _COMPOSITION_STEP,
            {"start": True, "value": "(ni)", "caret": 3, "data": "ni", "end": False},
        )
        # compositionupdate records the preedit end on a macrotask, as with a real IME.
        page.wait_for_timeout(100)
        composition_view = terminal_view.locator(".composition-view")
        expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
        expect(composition_view).to_have_text("ni")

        commit_baseline = len(sent)
        textarea.evaluate(
            _COMPOSITION_STEP,
            {
                "start": False,
                "value": f"({CANDIDATE})",
                "caret": 2,
                "data": CANDIDATE,
                "end": True,
            },
        )
        # Terminal replies (e.g. the OSC background-color report the theme
        # picker queries) can share the commit's capture window; strip them and
        # wait for the candidate so only the committed text is asserted.
        committed = _ANSI.sub(
            b"",
            _wait_for_frames(
                page,
                sent,
                commit_baseline,
                lambda b: CANDIDATE.encode() in _ANSI.sub(b"", b),
                timeout_s=5,
            ),
        ).decode("utf-8", "replace")

        outcomes = (b"())", f"({CANDIDATE})".encode())
        _wait_for_frames(
            page,
            received,
            echo_baseline,
            lambda _b: any(o in _echo_text(received, echo_baseline) for o in outcomes),
            timeout_s=10,
        )
        echoed = _echo_text(received, echo_baseline).decode("utf-8", "replace")
        if record_dir:
            # Hold the final frame so the committed candidate stays readable in
            # the clip; the assertions below never need this pause.
            page.wait_for_timeout(2_000)
            page.screenshot(path=os.path.join(record_dir, "ime-autopair-final.png"))

        # The two sent-frame facets below are the reported, program-independent
        # contract. The PTY echo is recorded for context only; the shell program
        # decides whether it renders a clean line.
        summary = (
            f"pair frames: {pair_frames!r}; cursor-left sent: {cursor_left_sent}; "
            f"composition commit: {committed!r}; PTY echo: {echoed!r}"
        )
        assert cursor_left_sent, (
            "no cursor-left followed the auto-inserted pair, so the PTY caret "
            f"sits after ')' — {summary}"
        )
        assert committed == CANDIDATE, (
            f"the composition committed {committed!r} instead of {CANDIDATE!r} — {summary}"
        )
    finally:
        context.close()
