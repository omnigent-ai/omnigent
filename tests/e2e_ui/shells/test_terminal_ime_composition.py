"""E2E: the terminal pane must honour IME composition.

Two defects can bite the embedded terminal's input path:

1. **Shift+Enter is claimed mid-composition, dropping the composed text.**
   ``terminalKeyEventPayload`` (``web/src/components/blocks/TerminalSession.ts``)
   returns the CSI-u sequence for Shift+Enter with no composition check, and
   the ``attachCustomKeyEventHandler`` caller ``preventDefault()``s the event
   and sends those bytes to the PTY. xterm consults the custom handler
   *before* its own ``CompositionHelper.keydown`` — the step that finalizes
   an in-flight composition and commits its text — so claiming the key means
   the composed text is never committed at all: a CSI-u frame goes to the PTY
   in place of the text the user converted.

2. **A synchronous echo path repaints over an uncommitted preedit.** The
   ``writeSync`` fast path this referred to was removed wholesale by
   "fix(web): keep terminal output on xterm's ordered write queue";
   every inbound frame now goes through xterm's ordered public write queue,
   the path its composition handling was built against.

Both journeys are driven here without a real IME: dispatching
``compositionstart`` / ``compositionupdate`` at ``term.textarea`` puts
xterm's ``CompositionHelper`` into a genuine composing
state (its listeners do not check ``isTrusted``), and the observable contract
is read off the attach WebSocket's sent/received frames plus xterm's
``.composition-view`` preedit overlay.

Like the rest of this directory, the shell is user-created from the workspace
rail's "+" menu — no LLM turn is involved.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The string the user has "converted" in the IME preedit when the key
# arrives. Any non-ASCII text works; kana keeps the journey honest.
COMPOSED_TEXT = "かんじ"

# Kitty Keyboard Protocol / CSI-u encoding the terminal claims Shift+Enter
# for (mirrors SHIFT_ENTER_CSI_U in web/src/components/blocks/TerminalSession.ts).
SHIFT_ENTER_CSI_U = b"\x1b[13;2u"


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


def _capture_attach_frames(page: Page) -> tuple[list[bytes], list[bytes]]:
    """Record every frame sent/received on terminal-attach WebSockets.

    Registered before navigation so neither the relay attach nor a later
    direct-loopback re-dial can slip through. Frames are normalized to
    bytes (keystrokes go up as binary; text frames are UTF-8 encoded).

    :param page: Playwright page, not yet navigated.
    :returns: ``(sent, received)`` lists that fill in as frames flow.
    """
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


def _begin_composition(textarea, text: str) -> None:
    """Put xterm's CompositionHelper into a real composing state.

    Mirrors what a browser does when an IME opens a preedit: a
    ``compositionstart``, the preedit text landing in the helper textarea,
    and a ``compositionupdate`` carrying it. xterm's listeners on
    ``term.textarea`` do not check ``isTrusted``, so its internal
    ``_isComposing`` / preedit bookkeeping runs exactly as with a real IME.
    """
    textarea.evaluate(
        """(ta, text) => {
          ta.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
          ta.value += text;
          ta.dispatchEvent(
            new CompositionEvent("compositionupdate", { data: text, bubbles: true })
          );
        }""",
        text,
    )


def test_shift_enter_mid_composition_commits_composed_text(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Shift+Enter during an IME composition must not drop the composed text.

    Journey: open a shell → focus the terminal → begin an IME composition
    (preedit ``かんじ``) → press Shift+Enter to commit-and-newline.

    Expected (what a native terminal does): the composed text is committed
    and reaches the PTY.

    Actual (the bug): ``terminalKeyEventPayload`` claims Shift+Enter with no
    composition check, so xterm's ``CompositionHelper.keydown`` — the code
    that finalizes the composition — never runs. The CSI-u frame is sent to
    the PTY and the composed text is dropped, never reaching the PTY at all.
    """
    base_url, session_id = terminal_session

    sent, _received = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)

    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()

    # Sanity: prove the frame capture and the input path are live before
    # asserting on an absence — a plain keystroke must show up as a sent
    # frame, otherwise the real assertion below could fail for capture
    # reasons rather than the bug.
    page.keyboard.type("q")
    assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
        f"attach WebSocket frame capture saw no keystroke frame; sent so far: {b''.join(sent)!r}"
    )

    _begin_composition(textarea, COMPOSED_TEXT)
    # compositionupdate records the preedit end position on a macrotask;
    # give it a beat, exactly as real IME event timing does.
    page.wait_for_timeout(100)

    # The preedit overlay is up: the composition is genuinely in flight.
    composition_view = terminal_view.locator(".composition-view")
    expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
    expect(composition_view).to_have_text(COMPOSED_TEXT)

    # The user commits the conversion with Shift+Enter. The event carries
    # isComposing — the same signal a real mid-composition keydown carries.
    textarea.evaluate(
        """(ta) => {
          ta.dispatchEvent(
            new KeyboardEvent("keydown", {
              key: "Enter",
              code: "Enter",
              shiftKey: true,
              isComposing: true,
              bubbles: true,
              cancelable: true,
            })
          );
        }""",
    )

    committed = _wait_for_sent_bytes(page, sent, COMPOSED_TEXT.encode("utf-8"), timeout_s=5)
    all_sent = b"".join(sent)
    assert committed, (
        "IME-composed text was dropped: it never reached the PTY after "
        "Shift+Enter mid-composition. "
        f"CSI-u claimed instead: {SHIFT_ENTER_CSI_U in all_sent}; "
        f"frames sent after focus: {all_sent!r}"
    )


def test_inbound_output_leaves_preedit_intact(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Inbound PTY output arriving mid-composition must not disturb the preedit.

    Journey: open a shell → type a command that emits output shortly after
    Enter (inside the old 750 ms post-keystroke window the removed
    ``writeSync`` echo path keyed on) → begin an IME composition immediately
    → the command's output arrives while the preedit is uncommitted.

    Expected: the preedit overlay survives the repaint — the composition
    stays active with its text intact, exactly as xterm's ordered public
    write queue (the only inbound write path) guarantees. Guards against
    a synchronous, composition-unaware paint path being reintroduced.
    """
    base_url, session_id = terminal_session

    _sent, received = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)

    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()

    # The quotes make the *output* differ from the echoed keystrokes, so the
    # marker below can only match the command's own output line.
    marker = "PREEDITREPAINTMARKER"
    page.keyboard.type('sleep 0.3; echo PREEDIT"REPAINT"MARKER')
    page.keyboard.press("Enter")

    # Begin composing immediately — well inside the window in which the
    # command's output will land on the pane.
    _begin_composition(textarea, COMPOSED_TEXT)
    composition_view = terminal_view.locator(".composition-view")
    expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
    expect(composition_view).to_have_text(COMPOSED_TEXT)

    baseline = len(received)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if marker.encode("utf-8") in b"".join(received[baseline:]):
            break
        page.wait_for_timeout(100)
    assert marker.encode("utf-8") in b"".join(received[baseline:]), (
        "command output never arrived while the composition was in flight; "
        "the journey did not exercise output-during-preedit"
    )

    # The output repainted the pane while the preedit was uncommitted; the
    # composition must still be live and showing the same preedit text.
    expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
    expect(composition_view).to_have_text(COMPOSED_TEXT)
    expect(terminal_view).to_have_attribute("data-state", "connected")
