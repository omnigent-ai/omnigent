"""E2E: terminal IME preedit must not be re-sent after a Shift+ASCII run.

Its sibling ``test_terminal_ime_composition.py`` guards Shift+Enter claimed
mid-composition; this guards the adjacent defect a Japanese IME user hits in
the terminal pane:

1. open a shell → focus the terminal
2. begin an IME composition
3. mid-line, use Shift to type an ASCII run (here a leading ``A``)
4. return to normal kana conversion and convert the next kana

Observed failure (this bug): the Shift+letter keydown reaches xterm's
``CompositionHelper.keydown`` and finalizes the composition early
(``_isComposing`` goes false), but the IME keeps composing without a fresh
``compositionstart``. Every following kana keydown (keyCode 229) then falls
into xterm's ``_handleAnyTextareaChanges``, which re-sends the whole helper
textarea — so the committed ``A`` prefix and the growing preedit are re-emitted
to the PTY on each update, and the romaji consonants leak through as fullwidth
latin (ｄ / ｙ / ｓ).

Correct behaviour: an in-flight preedit stays in the composition view; only
committed text reaches the PTY, each character once.

Driven without a real IME the same way the sibling test is: xterm's
``CompositionHelper`` listeners do not check ``isTrusted``, so the dispatched
``compositionstart`` / ``compositionupdate`` / keyboard events drive the real
xterm code path, and the contract is read off the attach WebSocket's sent
frames. Like the rest of this directory, the shell is user-created from the
workspace rail's "+" menu — no LLM turn is involved.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The committed ASCII run the user Shift-types mid-composition.
SHIFT_ASCII_PREFIX = "A"

# Fullwidth latin the romaji consonants show as while a Japanese IME converts
# them; none of these must ever reach the PTY (they are in-flight preedit).
FW_D = "ｄ"  # ｄ
FW_Y = "ｙ"  # ｙ
FW_S = "ｓ"  # ｓ
FULLWIDTH_ROMAJI = (FW_D, FW_Y, FW_S)

# Preedit progression after returning to kana conversion, targeting the line
# "Aでよいです": each entry is the FULL helper-textarea value (committed prefix
# + growing preedit) as the IME rewrites it, stepping through the fullwidth
# romaji then its kana.
PREEDIT_PROGRESSION = [
    SHIFT_ASCII_PREFIX + FW_D,
    SHIFT_ASCII_PREFIX + "で",
    SHIFT_ASCII_PREFIX + "で" + FW_Y,
    SHIFT_ASCII_PREFIX + "でよ",
    SHIFT_ASCII_PREFIX + "でよい",
    SHIFT_ASCII_PREFIX + "でよい" + FW_D,
    SHIFT_ASCII_PREFIX + "でよいで",
    SHIFT_ASCII_PREFIX + "でよいで" + FW_S,
    SHIFT_ASCII_PREFIX + "でよいです",
]
COMMITTED_LINE = SHIFT_ASCII_PREFIX + "でよいです"


def _open_new_shell(page: Page) -> None:
    """Create a shell via the workspace rail's "+" → Shell menu."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _connected_terminal(page: Page):
    """Wait for the newest terminal view to report a live attach."""
    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def _capture_attach_frames(page: Page) -> list[bytes]:
    """Record every frame sent on terminal-attach WebSockets.

    Registered before navigation so the relay attach cannot slip through.
    Keystrokes go up as binary; text frames are UTF-8 encoded.
    """
    sent: list[bytes] = []

    def _as_bytes(payload: str | bytes) -> bytes:
        return payload if isinstance(payload, bytes) else payload.encode("utf-8")

    def _on_ws(ws: object) -> None:
        if "/attach" not in ws.url:  # type: ignore[attr-defined]
            return
        ws.on(  # type: ignore[attr-defined]
            "framesent",
            lambda payload: sent.append(_as_bytes(payload)),
        )

    page.on("websocket", _on_ws)
    return sent


def _wait_for_sent_bytes(page: Page, sent: list[bytes], needle: bytes, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if needle in b"".join(sent):
            return True
        page.wait_for_timeout(100)
    return needle in b"".join(sent)


def test_shift_ascii_run_does_not_resend_preedit(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A Shift+ASCII run mid-composition must not make the preedit re-emit.

    See the module docstring for the journey. The bug re-sends the committed
    ``A`` prefix on every subsequent preedit update and leaks the fullwidth
    romaji consonants to the PTY; a correct terminal keeps the preedit in the
    composition view and sends only committed text, each character once.
    """
    base_url, session_id = terminal_session

    sent = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)

    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()

    # Prove the frame capture and input path are live before asserting on the
    # journey, so a real assertion below can't fail for capture reasons.
    page.keyboard.type("q")
    assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
        f"attach WebSocket frame capture saw no keystroke; sent so far: {b''.join(sent)!r}"
    )
    sent.clear()

    # Begin a composition, then commit a Shift-typed ASCII run 'A' with a
    # Shift+letter keydown mid-composition. xterm's CompositionHelper finalizes
    # the composition on this non-229 keydown and sends the 'A' once.
    textarea.evaluate(
        """(ta) => {
          ta.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
          ta.value = "A";
          ta.dispatchEvent(
            new CompositionEvent("compositionupdate", { data: "A", bubbles: true }),
          );
        }"""
    )
    page.wait_for_timeout(150)
    textarea.evaluate(
        """(ta) => {
          ta.dispatchEvent(new KeyboardEvent("keydown", {
            key: "A", code: "KeyA", keyCode: 65, shiftKey: true,
            isComposing: true, bubbles: true, cancelable: true,
          }));
        }"""
    )
    page.wait_for_timeout(200)

    # Return to kana conversion. The real IME does not fire a fresh
    # compositionstart here, so each kana keydown carries keyCode 229 and the
    # preedit is written into the helper textarea in the same browser turn.
    for value in PREEDIT_PROGRESSION:
        textarea.evaluate(
            """(ta, v) => {
              ta.dispatchEvent(new KeyboardEvent("keydown", {
                key: "Process", keyCode: 229, isComposing: true,
                bubbles: true, cancelable: true,
              }));
              ta.value = v;
            }""",
            value,
        )
        page.wait_for_timeout(120)
    textarea.evaluate(
        """(ta, v) => {
          ta.value = v;
          ta.dispatchEvent(new CompositionEvent("compositionend", { data: v, bubbles: true }));
        }""",
        COMMITTED_LINE,
    )
    page.wait_for_timeout(500)

    stream = b"".join(sent).decode("utf-8", "replace")

    # The in-flight preedit must never reach the PTY.
    leaked = [fw for fw in FULLWIDTH_ROMAJI if fw in stream]
    assert not leaked, (
        "IME preedit leaked to the PTY: fullwidth romaji "
        f"{leaked} reached the terminal after the Shift+ASCII run. "
        f"PTY stream: {stream!r}"
    )

    # The committed Shift-ASCII prefix must be sent once, not re-emitted on
    # every preedit update.
    prefix_count = stream.count(SHIFT_ASCII_PREFIX)
    assert prefix_count <= 1, (
        f"committed Shift+ASCII prefix {SHIFT_ASCII_PREFIX!r} was re-sent to the "
        f"PTY {prefix_count} times (expected at most once) — the preedit is being "
        f"re-emitted on every composition update. PTY stream: {stream!r}"
    )
