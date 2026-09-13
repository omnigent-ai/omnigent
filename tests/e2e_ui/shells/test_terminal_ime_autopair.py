"""E2E: an IME's automatic punctuation pair must not corrupt the next candidate.

A mobile touch keyboard that auto-inserts a punctuation pair writes ``()``
into xterm's helper textarea in one IME-processed keystroke (``keyCode 229``)
and leaves the caret *between* the pair (``selectionStart == 1``). xterm 6's
``CompositionHelper`` tracks the textarea by value length only, never by
caret, so the pair poisons the following Chinese composition:

1. ``_handleAnyTextareaChanges`` diffs old/new textarea values and sends the
   pair ``()`` to the PTY with no matching left movement for the in-pair
   caret — the terminal cursor sits after ``)`` while the IME composes
   before it.
2. ``compositionstart`` records the composition position as
   ``textarea.value.length`` (2, the end of the value) although the
   composition happens at the caret (1). When the user converts ``ni`` and
   selects 你, ``_finalizeComposition`` slices ``"(你)".substring(2)`` and
   sends ``)`` to the PTY instead of 你.

The user typed ``(`` and 你 expecting ``(你)``; the terminal receives
``())`` and the composed candidate never arrives.

Driven exactly like ``test_terminal_ime_composition.py`` in this directory:
the IME event stream is replayed synthetically at ``term.textarea`` (xterm's
listeners do not check ``isTrusted``), mirroring the controlled Chromium
replay in the report — ``keydown(229)`` → textarea ``()`` / selection 1 →
``input`` → ``keyup(229)`` → composition ``ni`` → textarea ``(你)`` /
selection 2 → ``compositionend(你)`` → final ``input``. Composition input
events carry ``insertCompositionText`` and are ``composed``, as trusted IME
events are, so xterm's emoji-IME fallback in ``_inputEvent`` stays out of the
way and the CompositionHelper owns the stream, exactly as with a real IME.
The observable contract is read off the attach WebSocket's sent frames.

Like the rest of this directory, the shell is user-created from the workspace
rail's "+" menu — no LLM turn is involved.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The candidate the user selects from the IME after composing "ni".
COMPOSED_CANDIDATE = "你"


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


def _ime_insert_autopair(textarea) -> None:
    """Replay an IME auto-inserting ``()`` with the caret left inside the pair.

    Mirrors the mobile keyboard's event stream byte for byte:
    ``keydown(229)`` → the textarea gains ``()`` with the caret between the
    pair → ``input(insertText)`` → ``keyup(229)``. The input event is
    ``composed`` (as every trusted UI event is) and lands between keydown
    and keyup, so xterm's emoji-IME fallback in ``_inputEvent`` ignores it
    and the CompositionHelper's textarea diff owns the keystroke, exactly
    as with a real IME.
    """
    textarea.evaluate(
        """(ta) => {
          const fire229 = (type) => {
            const ev = new KeyboardEvent(type, {
              key: "Process",
              bubbles: true,
              cancelable: true,
            });
            Object.defineProperty(ev, "keyCode", { get: () => 229 });
            ta.dispatchEvent(ev);
          };
          fire229("keydown");
          const base = ta.value.length;
          ta.value = ta.value + "()";
          ta.selectionStart = ta.selectionEnd = base + 1;
          ta.dispatchEvent(
            new InputEvent("input", {
              data: "()",
              inputType: "insertText",
              bubbles: true,
              composed: true,
            })
          );
          fire229("keyup");
        }"""
    )


def _ime_compose_update(textarea, prev: str, preedit: str) -> None:
    """Advance the composition preedit at the caret from *prev* to *preedit*.

    Starts the composition when *prev* is empty. The preedit replaces the
    previous preedit text at the caret — inside the pair — never at the end
    of the textarea value.
    """
    textarea.evaluate(
        """(ta, [prev, preedit]) => {
          const fire229 = (type) => {
            const ev = new KeyboardEvent(type, {
              key: "Process",
              bubbles: true,
              cancelable: true,
            });
            Object.defineProperty(ev, "keyCode", { get: () => 229 });
            ta.dispatchEvent(ev);
          };
          fire229("keydown");
          if (prev === "") {
            ta.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
          }
          const start = ta.selectionStart - prev.length;
          ta.value = ta.value.slice(0, start) + preedit + ta.value.slice(start + prev.length);
          ta.selectionStart = ta.selectionEnd = start + preedit.length;
          ta.dispatchEvent(
            new CompositionEvent("compositionupdate", { data: preedit, bubbles: true })
          );
          ta.dispatchEvent(
            new InputEvent("input", {
              data: preedit,
              inputType: "insertCompositionText",
              bubbles: true,
              composed: true,
            })
          );
          fire229("keyup");
        }""",
        [prev, preedit],
    )


def _ime_commit_candidate(textarea, prev: str, candidate: str) -> None:
    """Select *candidate* from the IME, replacing the *prev* preedit.

    Mirrors Chromium's commit stream: the textarea settles on the final
    text with the caret after the candidate (still inside the pair), then
    ``compositionend`` carries the candidate and a final composition input
    event follows.
    """
    textarea.evaluate(
        """(ta, [prev, candidate]) => {
          const start = ta.selectionStart - prev.length;
          ta.value = ta.value.slice(0, start) + candidate + ta.value.slice(start + prev.length);
          ta.selectionStart = ta.selectionEnd = start + candidate.length;
          ta.dispatchEvent(
            new CompositionEvent("compositionupdate", { data: candidate, bubbles: true })
          );
          ta.dispatchEvent(
            new CompositionEvent("compositionend", { data: candidate, bubbles: true })
          );
          ta.dispatchEvent(
            new InputEvent("input", {
              data: candidate,
              inputType: "insertCompositionText",
              bubbles: true,
              composed: true,
            })
          );
        }""",
        [prev, candidate],
    )


def test_autopair_then_composition_commits_candidate(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A composition following an auto-inserted pair must commit its candidate.

    Journey: open a shell → the touch keyboard auto-inserts ``()`` leaving
    the caret between the pair → compose ``ni`` and select 你 without moving
    the caret out of the pair.

    Expected (what a native terminal shows): ``(你)`` — the composed
    candidate reaches the PTY, and no unconfirmed preedit is ever sent.

    Actual (the bug): xterm's CompositionHelper anchors the composition at
    the end of the textarea value instead of the caret, so the PTY receives
    the pair ``()`` with no left movement and then ``)`` in place of 你 —
    the terminal shows ``())`` and the candidate never arrives.
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
    # reasons rather than the bug. The backspace erases the probe so the
    # pane shows only the journey's own bytes afterwards.
    page.keyboard.type("q")
    assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
        f"attach WebSocket frame capture saw no keystroke frame; sent so far: {b''.join(sent)!r}"
    )
    page.keyboard.press("Backspace")

    # The touch keyboard inserts the pair; the caret stays inside it.
    _ime_insert_autopair(textarea)
    # CompositionHelper diffs the textarea on a macrotask; give it a beat
    # (real IME event timing leaves the same gap before composition starts).
    page.wait_for_timeout(300)
    assert _wait_for_sent_bytes(page, sent, b"(", timeout_s=10), (
        "the auto-inserted pair never reached the PTY; the replay did not "
        f"engage the input path. Frames sent so far: {b''.join(sent)!r}"
    )

    # Compose "ni" at the in-pair caret. compositionupdate records the
    # preedit end position on a macrotask; pause between updates exactly as
    # real IME event timing does.
    _ime_compose_update(textarea, "", "n")
    page.wait_for_timeout(150)
    _ime_compose_update(textarea, "n", "ni")
    page.wait_for_timeout(150)

    # The preedit overlay is up: the composition is genuinely in flight.
    composition_view = terminal_view.locator(".composition-view")
    expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
    expect(composition_view).to_have_text("ni")

    # The user selects 你 from the candidate list; the caret never leaves
    # the pair.
    _ime_commit_candidate(textarea, "ni", COMPOSED_CANDIDATE)

    committed = _wait_for_sent_bytes(page, sent, COMPOSED_CANDIDATE.encode("utf-8"), timeout_s=5)
    all_sent = b"".join(sent)
    assert committed, (
        "IME candidate was corrupted: the composed 你 never reached the PTY "
        "after an automatic punctuation pair left the caret inside (). "
        f"Bytes sent after focus (decoded): {all_sent!r} — the byte after "
        "the pair is what the PTY got in place of the candidate."
    )

    # The uncommitted preedit must never leak to the PTY: only the pair,
    # any caret movement, and the committed candidate may go up.
    assert b"ni" not in all_sent, f"unconfirmed preedit was sent to the PTY: {all_sent!r}"

    # Hold the final pane state for a beat so a recorded run shows the
    # outcome the user sees.
    page.wait_for_timeout(1500)
