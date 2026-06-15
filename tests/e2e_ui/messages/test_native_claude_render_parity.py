r"""UI journey: a native Claude Code session renders parity with its TUI.

The native ``claude-native`` ("Claude Code") wrapper is terminal-first: a real
``claude`` CLI runs in the session terminal, the SPA's **Terminal** view
attaches to that live TUI over a WebSocket, and the SPA's **Chat** view renders
the SAME canonical transcript (``GET /v1/sessions/{id}/items``) the TUI prints.
A native bridge forwards web-composer messages INTO the Claude process and
forwards Claude's transcript back OUT as conversation items. This suite asserts
that round-trips both ways and renders exactly once — the three properties the
native forwarder has historically regressed on:

1. **Render parity with the TUI.** Composer turns are sent through the web SPA;
   each per-turn user marker and assistant token the SPA shows in a bubble must
   also appear in the canonical transcript, in the same order, exactly once. The
   transcript is what the TUI prints, so transcript parity == "renders the same
   as the TUI".

2. **A TUI-originated message surfaces in the web UI.** A turn is typed directly
   into the Claude Code TUI (the embedded xterm in the Terminal view) — never
   through the composer. The native bridge must forward it back out as a user
   item + assistant reply, so switching to Chat shows both as bubbles and the
   transcript carries them. This is the OUT direction the composer turns don't
   exercise.

3. **No duplicate rendering.** Every marker/token — composer- and TUI-originated
   alike — must land in EXACTLY ONE bubble and one transcript entry. The classic
   native-forwarder bug double-rendered a reply as both a streaming live preview
   and the committed bubble; per-turn unique tokens make that count unambiguous.

Per-turn unique markers/tokens are load-bearing for the same reasons as the
custom-agent suite: they keep the dedup count and order checks unambiguous even
though Claude Code is far chattier than the ``echo_probe`` agent (it may emit
thinking, tool calls, and prose around the echoed token). Every assertion counts
*bubbles/entries containing the token*, never bubbles, so chattiness is fine.

Requires the native-harness gateway auth the runner derives from its own
credentials (the same gateway ``hello_world`` uses); CI exchanges Databricks
OAuth before pytest. See ``native_claude_session`` in ``conftest.py``.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from playwright.sync_api import Page, expect

# Reuse the custom-agent suite's helpers — both surfaces render from the same
# canonical transcript, so parity / dedup / ordering are asserted identically.
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
# xterm.js routes all keystrokes through a hidden helper <textarea>; focusing it
# and typing is how a user (and Playwright) drives the embedded TUI.
_XTERM_INPUT = ".xterm-helper-textarea"

# A native Claude Code turn is a full agent loop (real LLM, possible tool use),
# so it is far slower than a single custom-agent LLM call.
_NATIVE_TURN_TIMEOUT_MS = 180_000
# Claude Code boots in the terminal on bind; the auto-launch + first-run
# pre-accept + WS attach can take a while on a cold CI runner.
_TERMINAL_READY_TIMEOUT_MS = 120_000

# Two composer turns (the IN direction) + one TUI turn (the OUT direction).
_COMPOSER_TURNS = 2


def _open_terminal_view(page: Page) -> None:
    """Switch a terminal-first session to its Terminal (TUI) view.

    :param page: The Playwright page, on the session's chat surface.
    """
    view_mode = page.get_by_role("group", name="View mode")
    expect(view_mode).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    terminal_button = view_mode.get_by_role("button", name="Terminal")
    expect(terminal_button).to_be_visible(timeout=30_000)
    terminal_button.click()


def _wait_terminal_connected(page: Page) -> None:
    """Wait until the embedded xterm has attached to the live Claude TUI.

    :param page: The Playwright page, on the Terminal view.
    """
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, text: str) -> None:
    """Type *text* into the embedded Claude Code TUI and submit with Enter.

    Drives the real TUI exactly as a user would: focus the xterm input,
    type the prompt, press Enter. This is the OUT direction — the message
    originates in the terminal, not the web composer.

    :param page: The Playwright page, on the connected Terminal view.
    :param text: The single-line prompt to type into the TUI.
    """
    xterm_input = page.locator(_XTERM_INPUT).last
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    # Type at a human-ish cadence: Claude Code's TUI composer can drop or
    # reorder characters injected faster than it repaints.
    page.keyboard.type(text, delay=15)
    page.keyboard.press("Enter")


def _dump_tui_screenshot(page: Page, tag: str) -> None:
    """TEMP diagnostic: screenshot the live Claude Code TUI pane.

    On a CI failure the default page screenshot shows the Chat view; this
    switches to the Terminal view and captures the xterm canvas so a
    blocking first-run TUI screen (trust/login/theme/api-key prompt) is
    visible in the uploaded artifact. Best-effort — never masks the real
    failure. REMOVE with the rest of the native-claude CI validation TEMP.

    :param page: The Playwright page.
    :param tag: Short label for the screenshot filename.
    """
    out_dir = os.environ.get("OMNIGENT_TUI_DIAG_DIR", "/tmp")
    try:
        _open_terminal_view(page)
        page.wait_for_timeout(1500)
        page.locator(_TERMINAL_VIEW).last.screenshot(path=f"{out_dir}/claude-tui-{tag}.png")
        _log.warning("captured Claude TUI screenshot: %s/claude-tui-%s.png", out_dir, tag)
    except Exception as exc:
        # Diagnostic must never mask the real failure.
        _log.warning("failed to capture Claude TUI screenshot (%s): %s", tag, exc)


@pytest.mark.timeout(900)
def test_native_claude_message_render_parity(
    page: Page,
    native_claude_session: tuple[str, str],
) -> None:
    """Native Claude Code renders parity with its TUI, both ways, with no dupes.

    Covers all three properties on a single (expensive to spin up) native
    session: composer parity (IN), a TUI-originated turn surfacing in the web UI
    (OUT), and no duplicate rendering across every turn.
    """
    base_url, session_id = native_claude_session
    _log.info("native-claude session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # The Terminal view proves Claude Code actually booted in the session
    # terminal (the runner's claude-native auto-launch) before we send anything.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Claude Code TUI attached (terminal-view connected)")

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
        _log.info(
            "composer turn %d: sending (marker=%s token=%s)", index, user_marker, assistant_token
        )
        _send(page, _turn_prompt(index, user_marker, assistant_token))
        # The echoed token in an assistant bubble = this turn produced its reply.
        try:
            expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
                timeout=_NATIVE_TURN_TIMEOUT_MS
            )
        except AssertionError:
            # TEMP: capture the TUI pane so a blocking first-run screen is visible.
            _dump_tui_screenshot(page, f"composer-turn-{index}")
            raise
        # Fully settle (working shimmer gone) before the next send, so any
        # transient native live-preview has collapsed into the committed bubble.
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_TURN_TIMEOUT_MS)
        expect(page.locator(_USER)).to_have_count(index, timeout=30_000)
        _log.info("composer turn %d: settled", index)

    # --- Property 2 & 3: a TUI-originated turn (OUT) surfaces in the web UI. ---
    tui_index = _COMPOSER_TURNS + 1
    tui_marker, tui_token = _new_turn(tui_index)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info(
        "TUI turn %d: typing into xterm (marker=%s token=%s)", tui_index, tui_marker, tui_token
    )
    _type_into_tui(page, _turn_prompt(tui_index, tui_marker, tui_token))

    # Back in Chat, the bridge must have forwarded the TUI turn OUT as a user
    # item + assistant reply — both render as bubbles, exactly once.
    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT, has_text=tui_token).first).to_be_visible(
        timeout=_NATIVE_TURN_TIMEOUT_MS
    )
    expect(page.locator(_USER, has_text=tui_marker).first).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_TURN_TIMEOUT_MS)
    expect(page.locator(_USER)).to_have_count(len(user_markers), timeout=30_000)
    _log.info("TUI turn %d: surfaced in web UI (user + assistant bubbles present)", tui_index)

    # --- Assert all three properties over every turn. ---
    _assert_no_duplicate_render(page, user_markers, assistant_tokens)
    _assert_transcript_parity(base_url, session_id, user_markers, assistant_tokens)
    _log.info("all turns verified: render parity + no-duplicate-render + transcript parity")
