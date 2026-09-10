"""E2E: a shared session's shell must respond to a non-owner collaborator.

Journey (owner = the headerless ``local`` user, Bob = an edit-level
collaborator):

1. the owner grants Bob edit access on a runner-bound, terminal-capable
   session and opens a ``zsh`` shell (the same REST call the tab strip's
   "+" -> Shell menu makes);
2. Bob opens the shared session, selects the shell tab in the workspace
   rail, and waits for its xterm to connect (output streams to him);
3. Bob types a command into the connected shell and presses Enter.

Expected: the shell responds — the typed command runs and its output
appears in Bob's terminal. The regression this guards against: every
non-owner (any level below owner, including edit and manage) attaching
user shells read-only — in the SPA and again server-side in
``_authorize_terminal_attach`` — so the collaborator's keystrokes are
silently dropped and the shell reads as unresponsive with no feedback
about why. Typing into a user-launched shell is workspace mutation the
edit level already grants (editors can open/close these shells and run
agent turns), so an editor's interactive attach must be honored; only
the agent's own pane stays owner-only.

Terminal output is observed on Bob's terminal-attach WebSocket frames
rather than the DOM: xterm renders to a WebGL canvas, so terminal text
never reaches the DOM (same constraint as ``shells/test_new_shell.py``).
The pre-typing frame check pins the read path (output DOES stream to a
non-owner), so a failure of the final assertion isolates the write path.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx
from playwright.sync_api import Browser, expect

from tests.e2e_ui.conftest import open_right_rail

# Permission level mirrored from omnigent/server/auth.py (READ=1, EDIT=2,
# MANAGE=3, OWNER=4). Edit is the strongest grantable collaborator level
# that can run agent turns, so it is the level a "shared session" user
# realistically holds.
_LEVEL_EDIT = 2


def test_shared_shell_responds_to_editor_input(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    base_url, session_id = terminal_session
    bob_email = f"bob-{uuid.uuid4().hex[:6]}@ui.test"

    # ── Owner side (headerless ``local``, REST only) ──────────────────
    # Grant Bob EDIT, then open a zsh shell — the same POST the tab
    # strip's "+" → Shell menu makes (generous timeout: first PTY spawn
    # may wake the runner).
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": bob_email, "level": _LEVEL_EDIT},
        timeout=10.0,
    ).raise_for_status()
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": f"u-{uuid.uuid4().hex[:6]}"},
        timeout=60.0,
    ).raise_for_status()

    marker = f"shared-shell-{uuid.uuid4().hex[:8]}"

    # ── Bob's browser (identified via X-Forwarded-Email) ──────────────
    ctx_kwargs: dict[str, Any] = {
        "extra_http_headers": {"X-Forwarded-Email": bob_email},
    }
    # Most e2e_ui recording plumbing patches the *async* Browser; this
    # test opens its context through the sync pytest-playwright fixture,
    # so wire the video dir explicitly when a recording is requested.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir
    bob_ctx = browser.new_context(**ctx_kwargs)
    try:
        page = bob_ctx.new_page()

        # Capture every terminal-attach WebSocket frame the page receives.
        # Terminal bytes only exist here — xterm paints them onto a WebGL
        # canvas, never into the DOM.
        received: list[str] = []

        def _on_websocket(ws: Any) -> None:
            if "/probe" in ws.url or "attach" not in ws.url:
                return

            def _on_frame(payload: Any) -> None:
                if isinstance(payload, (bytes, bytearray)):
                    received.append(bytes(payload).decode("utf-8", "replace"))
                else:
                    received.append(str(payload))

            ws.on("framereceived", _on_frame)

        page.on("websocket", _on_websocket)

        page.goto(f"{base_url}/c/{session_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")

        # The owner's shell surfaces as a rail tab ("zsh · u-…"). Select
        # it (idempotent if the fresh-shell effect already opened it) and
        # wait for its xterm to connect.
        tab = rail.locator('[role="button"][title^="zsh · u-"]').first
        expect(tab).to_be_visible(timeout=60_000)
        tab.click()
        terminal_view = rail.get_by_test_id("terminal-view").last
        expect(terminal_view).to_be_visible(timeout=60_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)

        # Read-path sanity: output (the shell prompt) streams to Bob.
        # Guards the final assertion against passing vacuously when frame
        # capture or the attach itself is broken.
        deadline = time.time() + 30
        while time.time() < deadline and not received:
            page.wait_for_timeout(250)
        assert received, (
            "Bob's terminal attach delivered no output frames at all - the "
            "read path is broken, so the input assertion below would be "
            "meaningless."
        )

        # Bob types a command. Focus xterm's hidden helper textarea first
        # (a container click does not reliably focus the WebGL canvas in
        # headless Chromium — same approach as shells/test_new_shell.py).
        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.keyboard.type(f"echo {marker}")
        page.keyboard.press("Enter")

        # The shell must respond: the command's output (and the PTY's echo
        # of the typed line) comes back over the attach socket. Frames can
        # split anywhere, so search the concatenated stream.
        def _marker_seen() -> bool:
            return marker in "".join(received)

        deadline = time.time() + 15
        while time.time() < deadline and not _marker_seen():
            page.wait_for_timeout(250)

        assert _marker_seen(), (
            f"Shell is unresponsive to a non-owner: an edit-level collaborator "
            f"typed 'echo {marker}' + Enter into the connected shared shell, "
            f"but the marker never came back over the terminal socket "
            f"({len(''.join(received))} bytes of output received, so the read "
            f"path works — the keystrokes were dropped, with no read-only "
            f"indication shown to the user)."
        )
    finally:
        bob_ctx.close()
