"""Exercise IME auto-pair realignment through the browser, server, and PTY.

A keyboard inserts ``()`` with the caret inside, then composes ``ni`` to 你.
xterm tracks composition by textarea value rather than caret; without
realignment the PTY receives ``())`` instead of ``(你)``. The test replays
IME events synthetically at xterm's textarea and inspects attach frames;
it does not validate a physical phone keyboard.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

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


def _ime_insert_autopair(textarea) -> None:
    """Replay a keyCode-229 pair insert with the caret between the pair."""
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
    """Replace the previous preedit at the in-pair caret."""
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
    """Commit the selected candidate inside the pair."""
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
    """Commit 你 inside an auto-pair without sending unconfirmed preedit."""
    base_url, session_id = terminal_session

    sent, _received = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)

    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()

    # Prove the input path is live before checking for missing candidate bytes.
    page.keyboard.type("q")
    assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
        f"attach WebSocket frame capture saw no keystroke frame; sent so far: {b''.join(sent)!r}"
    )
    page.keyboard.press("Backspace")

    _ime_insert_autopair(textarea)
    # CompositionHelper forwards the pair on a macrotask.
    page.wait_for_timeout(300)
    assert _wait_for_sent_bytes(page, sent, b"(", timeout_s=10), (
        "the auto-inserted pair never reached the PTY; the replay did not "
        f"engage the input path. Frames sent so far: {b''.join(sent)!r}"
    )

    # Let each preedit update settle before the next.
    _ime_compose_update(textarea, "", "n")
    page.wait_for_timeout(150)
    _ime_compose_update(textarea, "n", "ni")
    page.wait_for_timeout(150)

    composition_view = terminal_view.locator(".composition-view")
    expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
    expect(composition_view).to_have_text("ni")

    _ime_commit_candidate(textarea, "ni", COMPOSED_CANDIDATE)

    committed = _wait_for_sent_bytes(page, sent, COMPOSED_CANDIDATE.encode("utf-8"), timeout_s=5)
    all_sent = b"".join(sent)
    assert committed, (
        "IME candidate was corrupted: the composed 你 never reached the PTY "
        "after an automatic punctuation pair left the caret inside (). "
        f"Bytes sent after focus (decoded): {all_sent!r} — the byte after "
        "the pair is what the PTY got in place of the candidate."
    )

    # The preedit must never reach the PTY.
    assert b"ni" not in all_sent, f"unconfirmed preedit was sent to the PTY: {all_sent!r}"

    # Pair, cursor-left, candidate is the required PTY order.
    pair_at = all_sent.find(b"()")
    assert pair_at != -1, f"the pair was not sent intact: {all_sent!r}"
    left_positions = [
        pos for pos in (all_sent.find(seq, pair_at) for seq in (b"\x1b[D", b"\x1bOD")) if pos != -1
    ]
    assert left_positions, f"no realigning cursor-left followed the pair: {all_sent!r}"
    assert all_sent.find(COMPOSED_CANDIDATE.encode("utf-8"), min(left_positions)) != -1, (
        f"the candidate did not follow the cursor-left: {all_sent!r}"
    )
    assert b"())" not in all_sent, f"the corrupted '())' stream was sent: {all_sent!r}"

    # Keep the result visible in recordings.
    page.wait_for_timeout(1500)
