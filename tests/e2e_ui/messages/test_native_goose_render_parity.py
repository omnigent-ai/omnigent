r"""UI journey: a native Goose session renders parity with its TUI.

The native ``goose-native`` ("Goose") wrapper is terminal-first: a real
``goose session`` CLI runs in the session terminal, the SPA's **Terminal** view
attaches to that live TUI over a WebSocket, and the SPA's **Chat** view renders
the SAME canonical transcript the TUI prints. A native forwarder
(:mod:`omnigent.goose_native_forwarder`) tails Goose's SQLite session store and
mirrors the transcript back OUT as conversation items; web-composer messages are
injected INTO the TUI's tmux pane by
:class:`omnigent.inner.goose_native_executor.GooseNativeExecutor`. This suite is
the goose sibling of ``test_native_cursor_render_parity`` and asserts the same
three properties:

1. **Render parity with the TUI.** Composer turns are sent through the web SPA;
   each per-turn user marker and assistant token must also appear in the
   canonical transcript, in order, exactly once.
2. **A TUI-originated message surfaces in the web UI.** A turn typed directly
   into the Goose TUI must be mirrored back out as a user item + assistant reply.
3. **No duplicate rendering.** Every marker/token lands in exactly one bubble.

Gating
------
Like cursor-native, Goose authenticates from its own config (``goose
configure`` → keyring / ``~/.config/goose/config.yaml``), which CI does not
provision. The suite **skips** when ``goose``/``tmux`` are absent or no Goose
provider config is present, and runs for real where Goose is configured.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from .test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _assert_no_duplicate_render,
    _assert_transcript_parity,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)

_log = logging.getLogger(__name__)

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_XTERM_INPUT = ".xterm-helper-textarea"

_NATIVE_TURN_TIMEOUT_MS = 180_000
_TERMINAL_READY_TIMEOUT_MS = 120_000
_COMPOSER_TURNS = 2


def _goose_unavailable_reason() -> str | None:
    """Return a skip reason when the goose-native prerequisites are absent.

    goose-native needs the ``goose`` binary + ``tmux`` on PATH and a usable Goose
    provider configuration (``~/.config/goose/config.yaml`` or a ``GOOSE_*``
    provider env). Any missing → a clean skip (CI provisions no Goose account).

    :returns: A human-readable skip reason, or ``None`` when prerequisites exist.
    """
    if shutil.which("goose") is None:
        return "goose-native render-parity needs the `goose` binary on PATH."
    if shutil.which("tmux") is None:
        return "goose-native render-parity needs `tmux` on PATH (runner-owned TUI pane)."
    has_config = (Path.home() / ".config" / "goose" / "config.yaml").is_file()
    import os as _os

    has_env = bool(_os.environ.get("GOOSE_PROVIDER"))
    if not (has_config or has_env):
        return (
            "goose-native render-parity needs a Goose provider: run `goose configure` "
            "or set GOOSE_PROVIDER/GOOSE_MODEL (+ a provider key). Skipped (not failed) "
            "because CI does not provision a Goose account by default."
        )
    return None


pytestmark = pytest.mark.skipif(
    _goose_unavailable_reason() is not None,
    reason=_goose_unavailable_reason() or "",
)


def _open_terminal_view(page: Page) -> None:
    """Switch a terminal-first session to its Terminal (TUI) view."""
    view_mode = page.get_by_role("group", name="View mode")
    expect(view_mode).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    terminal_button = view_mode.get_by_role("button", name="Terminal")
    expect(terminal_button).to_be_visible(timeout=30_000)
    terminal_button.click()


def _wait_terminal_connected(page: Page) -> None:
    """Wait until the embedded xterm has attached to the live Goose TUI."""
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, text: str) -> None:
    """Type *text* into the embedded Goose TUI and submit with Enter.

    Goose's REPL submits on Enter (Ctrl+J inserts a newline), so a single Enter
    after a short settle sends exactly one turn.
    """
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type(text, delay=15)
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")


def _wait_marker_in_transcript(
    base_url: str, session_id: str, marker: str, *, timeout_ms: int
) -> None:
    """Poll the canonical transcript until *marker* appears (TUI turn forwarded)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
            timeout=10.0,
        )
        if resp.status_code == 200 and any(
            marker in str(item.get("content")) for item in resp.json().get("data", [])
        ):
            return
        time.sleep(2.0)
    raise AssertionError(
        f"marker {marker!r} never reached the transcript within {timeout_ms}ms — "
        f"the TUI-typed turn was not submitted/forwarded for {session_id}."
    )


@pytest.mark.timeout(900)
def test_native_goose_message_render_parity(
    page: Page,
    native_goose_session: tuple[str, str],
) -> None:
    """Native Goose renders parity with its TUI, both ways, with no dupes.

    Mirrors ``test_native_cursor_message_render_parity``: composer parity (IN), a
    TUI-originated turn surfacing in the web UI (OUT), and no duplicate rendering.
    """
    base_url, session_id = native_goose_session
    _log.info("native-goose session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Goose TUI attached (terminal-view connected)")

    user_markers: list[str] = []
    assistant_tokens: list[str] = []

    def _new_turn(index: int) -> tuple[str, str]:
        nonce = uuid.uuid4().hex[:8]
        user_marker = f"usr-{index}-{nonce}"
        assistant_token = f"ast-{index}-{nonce}"
        user_markers.append(user_marker)
        assistant_tokens.append(assistant_token)
        return user_marker, assistant_token

    # --- Property 1 & 3: composer turns (IN) render parity, no dupes. ---
    _ensure_chat_view(page)
    for index in range(1, _COMPOSER_TURNS + 1):
        user_marker, assistant_token = _new_turn(index)
        _send(page, _turn_prompt(index, user_marker, assistant_token))
        expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
            timeout=_NATIVE_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_TURN_TIMEOUT_MS)
        expect(page.locator(_USER)).to_have_count(index, timeout=30_000)

    # --- Property 2 & 3: a TUI-originated turn (OUT) surfaces in the web UI. ---
    tui_index = _COMPOSER_TURNS + 1
    tui_marker, tui_token = _new_turn(tui_index)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, _turn_prompt(tui_index, tui_marker, tui_token))
    _wait_marker_in_transcript(base_url, session_id, tui_token, timeout_ms=_NATIVE_TURN_TIMEOUT_MS)

    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT, has_text=tui_token).first).to_be_visible(
        timeout=_NATIVE_TURN_TIMEOUT_MS
    )
    expect(page.locator(_USER, has_text=tui_marker).first).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_TURN_TIMEOUT_MS)
    expect(page.locator(_USER)).to_have_count(len(user_markers), timeout=30_000)

    # --- Assert all three properties over every turn. ---
    _assert_no_duplicate_render(page, user_markers, assistant_tokens)
    _assert_transcript_parity(base_url, session_id, user_markers, assistant_tokens)
    _log.info("all turns verified: render parity + no-duplicate-render + transcript parity")
