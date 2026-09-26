"""Exercise IME auto-pair correction in a mobile-sized browser and live PTY.

A touch keyboard can append ``()`` with the caret inside. xterm tracks
composition by value length, so without realignment it commits the trailing
``)`` instead of 你. This test replays IME events in a phone-profile
Chromium context; it does not emulate a physical phone keyboard.
"""

from __future__ import annotations

import os
import re
import time

from playwright.sync_api import Browser, Page, ViewportSize, expect

# Below the mobile navigation breakpoint.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

_CANDIDATE = "你"

# Replay a keyCode-229 insert with the caret inside the pair.
_AUTO_PAIR_REPLAY = """(ta) => {
  const key = (type) => {
    const ev = new KeyboardEvent(type, {
      key: "Process", bubbles: true, cancelable: true,
    });
    Object.defineProperty(ev, "keyCode", { value: 229 });
    return ev;
  };
  ta.dispatchEvent(key("keydown"));
  ta.value += "()";
  ta.selectionStart = ta.selectionEnd = ta.value.length - 1;
  ta.dispatchEvent(new InputEvent("input", {
    data: "()", inputType: "insertText", bubbles: true, composed: true,
  }));
  ta.dispatchEvent(key("keyup"));
}"""

# Replace the previous preedit at the caret.
_COMPOSITION_STEP = """(ta, { prev, next }) => {
  const start = ta.selectionStart - prev.length;
  ta.dispatchEvent(new CompositionEvent("compositionupdate", { data: next, bubbles: true }));
  ta.value = ta.value.slice(0, start) + next + ta.value.slice(start + prev.length);
  ta.selectionStart = ta.selectionEnd = start + next.length;
  ta.dispatchEvent(new InputEvent("input", {
    data: next, inputType: "insertCompositionText", bubbles: true, composed: true,
  }));
}"""

_COMPOSITION_END = """(ta, data) => {
  ta.dispatchEvent(new CompositionEvent("compositionend", { data, bubbles: true }));
  ta.dispatchEvent(new InputEvent("input", {
    data, inputType: "insertCompositionText", bubbles: true, composed: true,
  }));
}"""


def _capture_attach_frames(page: Page) -> tuple[list[bytes], list[bytes]]:
    """Capture attach frames before navigation, including later re-dials."""
    sent: list[bytes] = []
    received: list[bytes] = []

    def _as_bytes(payload: str | bytes) -> bytes:
        return payload if isinstance(payload, bytes) else payload.encode("utf-8")

    def _on_ws(ws: object) -> None:
        url = ws.url  # type: ignore[attr-defined]
        if "/attach" not in url:
            return
        ws.on("framesent", lambda payload: sent.append(_as_bytes(payload)))  # type: ignore[attr-defined]
        ws.on("framereceived", lambda payload: received.append(_as_bytes(payload)))  # type: ignore[attr-defined]

    page.on("websocket", _on_ws)
    return sent, received


def _wait_for_sent_bytes(page: Page, sent: list[bytes], needle: bytes, timeout_s: float) -> bool:
    """Poll until *needle* appears in the concatenated sent frames."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if needle in b"".join(sent):
            return True
        page.wait_for_timeout(100)
    return needle in b"".join(sent)


def _open_new_shell_mobile(page: Page) -> None:
    """Open a shell the way a phone user does: kebab → Shells → New shell."""
    page.get_by_role("button", name="Conversation actions").click()
    shells_entry = page.get_by_role("menuitem", name="Shells", exact=True)
    expect(shells_entry).to_be_visible(timeout=10_000)
    shells_entry.click()
    drawer = page.get_by_test_id("shells-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")
    drawer.get_by_role("button", name="New shell").click()


def test_ime_autopair_then_candidate_reaches_pty(
    browser: Browser, terminal_session: tuple[str, str]
) -> None:
    """Commit the candidate between an auto-pair through the mobile shell UI."""
    base_url, session_id = terminal_session

    context_kwargs: dict[str, object] = {
        "viewport": _MOBILE_VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    # Opt the sync browser context into recording when requested.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**context_kwargs)
    try:
        page = context.new_page()
        sent, _received = _capture_attach_frames(page)
        page.goto(f"{base_url}/c/{session_id}")

        _open_new_shell_mobile(page)
        terminal_view = page.locator('[data-testid="terminal-view"]:visible').first
        expect(terminal_view).to_be_visible(timeout=60_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)

        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()

        # Prove the input path is live before checking candidate bytes.
        page.keyboard.type("q")
        assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
            "attach WebSocket frame capture saw no keystroke frame; "
            f"sent so far: {b''.join(sent)!r}"
        )
        page.keyboard.press("Backspace")

        textarea.evaluate(_AUTO_PAIR_REPLAY)
        assert _wait_for_sent_bytes(page, sent, b"()", timeout_s=5), (
            "the auto-pair itself never reached the PTY; the replay did not "
            f"drive xterm's input path. Sent so far: {b''.join(sent)!r}"
        )

        # Compose "ni" at the in-pair caret.
        textarea.evaluate(
            '(ta) => ta.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }))'
        )
        textarea.evaluate(_COMPOSITION_STEP, {"prev": "", "next": "n"})
        page.wait_for_timeout(100)
        textarea.evaluate(_COMPOSITION_STEP, {"prev": "n", "next": "ni"})
        page.wait_for_timeout(100)

        composition_view = terminal_view.locator(".composition-view")
        expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
        expect(composition_view).to_have_text("ni")

        textarea.evaluate(_COMPOSITION_STEP, {"prev": "ni", "next": _CANDIDATE})
        textarea.evaluate(_COMPOSITION_END, _CANDIDATE)

        committed = _wait_for_sent_bytes(page, sent, _CANDIDATE.encode("utf-8"), timeout_s=5)
        # Let the PTY echo paint before judging.
        page.wait_for_timeout(1_000)
        all_sent = b"".join(sent)
        assert committed, (
            "the composed candidate never reached the PTY: the auto-pair's "
            "caret position was dropped and the composition commit picked up "
            f"the trailing ')'. Input sent after focus: {all_sent!r}"
        )
        assert b"())" not in all_sent, (
            "the terminal sent the corrupted '())' byte stream to the PTY "
            f"instead of keeping the candidate inside the pair: {all_sent!r}"
        )
        # Pair, cursor-left, candidate is the required PTY order.
        pair_at = all_sent.find(b"()")
        assert pair_at != -1, f"the pair was not sent intact: {all_sent!r}"
        left_positions = [
            pos
            for pos in (all_sent.find(seq, pair_at) for seq in (b"\x1b[D", b"\x1bOD"))
            if pos != -1
        ]
        assert left_positions, f"no realigning cursor-left followed the pair: {all_sent!r}"
        assert all_sent.find(_CANDIDATE.encode("utf-8"), min(left_positions)) != -1, (
            f"the candidate did not follow the cursor-left: {all_sent!r}"
        )
    finally:
        context.close()
