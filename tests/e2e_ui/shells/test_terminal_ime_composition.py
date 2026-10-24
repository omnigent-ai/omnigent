"""E2E: the terminal pane must honour IME composition.

Three defects can bite the embedded terminal's input path:

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

3. **A Shift+ASCII run mid-composition re-sends the committed prefix and the
   growing preedit on every later update.** A printable key typed while the
   IME is composing must not make xterm finalize the preedit early; the
   composed text is committed once, at ``compositionend``.

The journeys are driven here without a real IME: dispatching
``compositionstart`` / ``compositionupdate`` at ``term.textarea`` puts
xterm's ``CompositionHelper`` into a genuine composing state (its listeners
do not check ``isTrusted``), and the observable contract is read off the
attach WebSocket's sent/received frames plus xterm's ``.composition-view``
preedit overlay.

Like the rest of this directory, the shell is user-created from the workspace
rail's "+" menu — no LLM turn is involved.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Locator, Page, expect

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


def _wait_for_sent_quiescence(
    page: Page, sent: list[bytes], quiet_ms: int = 500, timeout_s: float = 3
) -> bool:
    """Wait until no frame has been sent for *quiet_ms*; ``False`` if *timeout_s* elapses first."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        count = len(sent)
        page.wait_for_timeout(quiet_ms)
        if len(sent) == count:
            return True
    return False


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


# The kana tail converted after the Shift-typed leading ASCII "A". A
# fullwidth-latin IME mode shows unconverted consonants as fullwidth latin
# (ｄ, ｙ, ｓ) in the preedit until each kana converts.
_SHIFT_ASCII_PREEDITS = [
    "A",
    "Aｄ",
    "Aで",
    "Aでｙ",
    "Aでよ",
    "Aでよｉ",
    "Aでよい",
    "Aでよいｄ",
    "Aでよいで",
    "Aでよいでｓ",
    "Aでよいです",
]
_SHIFT_ASCII_CODES = [
    "KeyA",
    "KeyD",
    "KeyE",
    "KeyY",
    "KeyO",
    "KeyI",
    "KeyI",
    "KeyD",
    "KeyE",
    "KeyS",
    "KeyU",
]
_COMMITTED_TAIL = "でよいです"
_COMMITTED_LINE = "A" + _COMMITTED_TAIL


def _fullwidth_latin(text: str) -> list[str]:
    return [c for c in text if 0xFF01 <= ord(c) <= 0xFF5E]


def _focused_shell_input(
    page: Page, terminal_session: tuple[str, str]
) -> tuple[list[bytes], Locator]:
    """Open a shell, focus xterm's helper textarea and prove the capture is live.

    Returns the captured sent frames and the focused textarea once a plain
    keystroke has shown up on the attach WebSocket, so the assertions that
    follow cannot fail for capture reasons.
    """
    base_url, session_id = terminal_session
    sent, _received = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type("q")
    assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
        f"attach WebSocket frame capture saw no keystroke frame; sent so far: {b''.join(sent)!r}"
    )
    return sent, textarea


def _drive_composition(
    textarea: Locator, preedits: list[str], codes: list[str], *, shift_ascii_first: bool
) -> bool:
    """Replay an IME composition at ``term.textarea`` without a real IME.

    A ``compositionstart`` opens the preedit. With *shift_ascii_first*, the
    first preedit is a Shift-typed ASCII letter whose keydown carries the real
    keyCode and ``isComposing`` — the mid-composition gesture under test. Every
    other keystroke is a keyCode-229 keydown with the preedit growing, and a
    ``compositionend`` commits. The order is a constructed regression sequence
    rather than a byte-exact native replay: the first preedit is written before
    the Shift+letter keydown so a premature finalization has a nonempty prefix
    to send, and no fresh ``compositionstart`` follows it, matching the
    reporter's trusted-CDP trace.

    :returns: Whether every keydown stayed uncanceled (``dispatchEvent`` returned
        ``true``). The IME must keep receiving the keys it owns, so the terminal
        may claim them from xterm but never ``preventDefault()`` them.
    """
    return textarea.evaluate(
        """(ta, args) => {
          const [preedits, codes, shiftAsciiFirst] = args;
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          const upd = (t) => {
            ta.value = t;
            ta.dispatchEvent(
              new CompositionEvent("compositionupdate", { data: t, bubbles: true })
            );
          };
          const kd = (o) =>
            ta.dispatchEvent(
              new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...o })
            );
          return (async () => {
            let uncanceled = true;
            ta.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
            let i = 0;
            if (shiftAsciiFirst) {
              upd(preedits[0]);
              await sleep(90);
              const shiftOk = kd({
                key: preedits[0],
                code: codes[0],
                keyCode: preedits[0].toUpperCase().charCodeAt(0),
                shiftKey: true,
                isComposing: true,
              });
              uncanceled = uncanceled && shiftOk;
              await sleep(90);
              i = 1;
            }
            for (; i < preedits.length; i++) {
              const ok = kd({ key: "Process", code: codes[i], keyCode: 229, isComposing: true });
              uncanceled = uncanceled && ok;
              upd(preedits[i]);
              await sleep(90);
            }
            const enterOk = kd({ key: "Process", code: "Enter", keyCode: 229, isComposing: true });
            uncanceled = uncanceled && enterOk;
            ta.value = preedits[preedits.length - 1];
            ta.dispatchEvent(
              new CompositionEvent("compositionend", { data: ta.value, bubbles: true })
            );
            return uncanceled;
          })();
        }""",
        [preedits, codes, shift_ascii_first],
    )


def test_shift_ascii_run_mid_composition_does_not_resend_preedit(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A Shift+ASCII run mid-composition must not re-send the preedit.

    Journey: open a shell → focus the terminal → begin an IME composition and
    Shift-type a leading ASCII "A" mid-composition → return to kana conversion
    and convert the tail ``でよいです`` → commit. No fresh ``compositionstart``
    fires after the Shift+letter, mirroring a native IME.

    Expected: the PTY receives ``Aでよいです`` exactly once, no fullwidth romaji
    leaks out of the preedit, and no composing keydown is ``preventDefault()``-ed
    away from the IME. The bug re-sent the committed prefix plus the growing
    preedit on every update (``AｄAでｙAでよ…``).
    """
    sent, textarea = _focused_shell_input(page, terminal_session)

    baseline = len(sent)
    uncanceled = _drive_composition(
        textarea, _SHIFT_ASCII_PREEDITS, _SHIFT_ASCII_CODES, shift_ascii_first=True
    )
    assert uncanceled, "a composing keydown was preventDefault()-ed away from the IME"
    assert _wait_for_sent_bytes(page, sent, _COMMITTED_TAIL.encode("utf-8"), timeout_s=10), (
        f"the converted kana never reached the PTY; sent: {b''.join(sent[baseline:])!r}"
    )
    # Let any erroneous re-sends that trail the commit land before asserting.
    assert _wait_for_sent_quiescence(page, sent), (
        f"the terminal kept sending frames after the commit: {b''.join(sent[baseline:])!r}"
    )

    decoded = b"".join(sent[baseline:]).decode("utf-8", "replace")
    assert decoded.count(_COMMITTED_TAIL) == 1, (
        "the committed prefix/preedit was re-sent to the PTY on composition updates: "
        f"{_COMMITTED_TAIL!r} appears {decoded.count(_COMMITTED_TAIL)} times in {decoded!r}"
    )
    assert decoded.count(_COMMITTED_LINE) == 1 and decoded.count("A") == 1, (
        f"the PTY must receive {_COMMITTED_LINE!r} exactly once; got {decoded!r}"
    )
    assert not _fullwidth_latin(decoded), (
        "fullwidth romaji consonants leaked out of the preedit to the PTY: "
        f"{_fullwidth_latin(decoded)} in {decoded!r}"
    )


def test_kana_conversion_without_shift_ascii_sends_once(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Control: the same kana conversion with no Shift+ASCII run sends once.

    Pins the trigger to the Shift+ASCII-mid-composition step: an ordinary
    composition of ``でよいです`` must reach the PTY exactly once with no
    fullwidth leak, so the failing scenario above cannot be blamed on IME
    composition itself.
    """
    sent, textarea = _focused_shell_input(page, terminal_session)

    baseline = len(sent)
    preedits = ["で", "でよ", "でよい", "でよいで", "でよいです"]
    codes = ["KeyD", "KeyO", "KeyI", "KeyE", "KeyU"]
    assert _drive_composition(textarea, preedits, codes, shift_ascii_first=False), (
        "a composing keydown was preventDefault()-ed away from the IME"
    )
    assert _wait_for_sent_bytes(page, sent, _COMMITTED_TAIL.encode("utf-8"), timeout_s=10), (
        f"the converted kana never reached the PTY; sent: {b''.join(sent[baseline:])!r}"
    )
    assert _wait_for_sent_quiescence(page, sent), (
        f"the terminal kept sending frames after the commit: {b''.join(sent[baseline:])!r}"
    )

    decoded = b"".join(sent[baseline:]).decode("utf-8", "replace")
    assert decoded.count(_COMMITTED_TAIL) == 1, (
        f"{_COMMITTED_TAIL!r} should be sent exactly once; got {decoded!r}"
    )
    assert not _fullwidth_latin(decoded), f"unexpected fullwidth leak: {decoded!r}"
