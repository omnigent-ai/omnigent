r"""UI journey: a native Cursor session renders parity with its TUI.

The native ``cursor-native`` ("Cursor") wrapper is terminal-first: a real
``cursor-agent`` CLI runs in the session terminal, the SPA's **Terminal** view
attaches to that live TUI over a WebSocket, and the SPA's **Chat** view renders
the SAME canonical transcript (``GET /v1/sessions/{id}/items``) the TUI prints.
A native forwarder (:mod:`omnigent.cursor_native_forwarder`) tails
``cursor-agent``'s own chat store and mirrors the transcript back OUT as
conversation items; web-composer messages are injected INTO the TUI's tmux pane
by :class:`omnigent.inner.cursor_native_executor.CursorNativeExecutor`. This
suite asserts that round-trips both ways and renders exactly once — the same
three properties the codex/claude native forwarders are pinned against
(:mod:`tests.e2e_ui.messages.test_native_codex_render_parity`):

1. **Render parity with the TUI.** Composer turns are sent through the web SPA;
   each per-turn user marker and assistant token the SPA shows in a bubble must
   also appear in the canonical transcript, in the same order, exactly once.

2. **A TUI-originated message surfaces in the web UI.** A turn is typed directly
   into the Cursor TUI (the embedded xterm in the Terminal view) — never through
   the composer. The forwarder must mirror it back out as a user item +
   assistant reply, so switching to Chat shows both as bubbles.

3. **No duplicate rendering.** Every marker/token — composer- and TUI-originated
   alike — must land in EXACTLY ONE bubble and one transcript entry.

How this differs from the claude/codex render-parity suites
-----------------------------------------------------------
Claude Code and Codex derive their model auth from the runner's own Databricks
gateway credentials (CI exchanges Databricks OAuth before pytest), so those
suites run unconditionally in CI. ``cursor-agent`` has **no Databricks-gateway
path** — it talks only to Cursor's backend and authenticates from the ambient
``cursor-agent login`` (``$HOME/.cursor``) or an ambient ``CURSOR_API_KEY``.
Because CI does not provision a Cursor account by default, this suite is **gated
to skip** when ``cursor-agent`` is absent or no usable Cursor login is present
(see :func:`_cursor_unavailable_reason`); it runs for real wherever Cursor is
logged in (a dev box, or a CI job that installs ``cursor-agent`` and provides a
``CURSOR_API_KEY`` secret). The ``native_cursor_session`` fixture launches the
TUI with ``-f`` so the unattended tmux pane never blocks on Cursor's
workspace-trust / per-tool approval prompts.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path

import httpx
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

# A native Cursor turn is a full agent loop (real LLM, possible tool use),
# so it is far slower than a single custom-agent LLM call.
_NATIVE_TURN_TIMEOUT_MS = 180_000
# cursor-agent boots in the terminal on bind; the auto-launch + first-run
# trust pre-accept + WS attach can take a while on a cold runner.
_TERMINAL_READY_TIMEOUT_MS = 120_000

# Two composer turns (the IN direction) + one TUI turn (the OUT direction).
_COMPOSER_TURNS = 2


def _cursor_unavailable_reason() -> str | None:
    """Return a skip reason when the cursor-native prerequisites are absent.

    cursor-native needs (1) the ``cursor-agent`` binary on PATH and (2) a usable
    Cursor login — either an ambient ``CURSOR_API_KEY`` or a prior
    ``cursor-agent login`` whose state lives under ``$HOME/.cursor``. Either
    missing is a clean **skip** (CI does not provision a Cursor account), so the
    e2e-ui shards stay green while the suite runs for real wherever Cursor is
    logged in.

    :returns: A human-readable skip reason, or ``None`` when both prerequisites
        are present.
    """
    if shutil.which("cursor-agent") is None:
        return "cursor-native render-parity needs the `cursor-agent` binary on PATH."
    if shutil.which("tmux") is None:
        return "cursor-native render-parity needs `tmux` on PATH (runner-owned TUI pane)."
    has_api_key = bool(os.environ.get("CURSOR_API_KEY"))
    has_login = (Path.home() / ".cursor").is_dir()
    if not (has_api_key or has_login):
        return (
            "cursor-native render-parity needs a Cursor login: export CURSOR_API_KEY "
            "or run `cursor-agent login` (state under $HOME/.cursor). Skipped (not "
            "failed) because CI does not provision a Cursor account by default."
        )
    return None


pytestmark = pytest.mark.skipif(
    _cursor_unavailable_reason() is not None,
    reason=_cursor_unavailable_reason() or "",
)


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
    """Wait until the embedded xterm has attached to the live Cursor TUI.

    :param page: The Playwright page, on the Terminal view.
    """
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, text: str) -> None:
    """Type *text* into the embedded Cursor TUI and submit with Enter.

    Drives the real TUI exactly as a user would: focus the xterm input,
    type the prompt, press Enter. This is the OUT direction — the message
    originates in the terminal, not the web composer.

    :param page: The Playwright page, on the connected Terminal view.
    :param text: The single-line prompt to type into the TUI.
    """
    # Scope to the active terminal-view (not page-level) so the lookup can't
    # focus a stray textarea from another terminal widget — matches the shell
    # E2E test pattern.
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    # Type at a human-ish cadence: the TUI composer can drop or reorder
    # characters injected faster than it repaints.
    page.keyboard.type(text, delay=15)
    # Let cursor-agent's composer fully register the typed text before Enter.
    # Unlike Codex's, it debounces input, so an Enter pressed in the same tick
    # as the last character can fire before the text is committed — the
    # composer then submits empty / stale and the turn is never sent. A short
    # settle pause makes the submission reliable.
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")


def _wait_marker_in_transcript(
    base_url: str, session_id: str, marker: str, *, timeout_ms: int
) -> None:
    """Poll the canonical transcript until *marker* appears in any item.

    Used after typing into the TUI to confirm the Enter submission actually
    reached ``cursor-agent`` (the forwarder only mirrors the turn once Cursor
    has accepted and stored it). This MUST complete before the test leaves the
    Terminal view: switching views tears down the embedded xterm's WebSocket,
    and cursor-agent's composer commits a submission slightly slower than
    Codex's — switching too early can drop the in-flight Enter keystroke before
    it reaches the tmux pane, so the turn is never sent. Waiting on the
    transcript (not a fixed sleep) keeps this deterministic.

    :param base_url: Spawned server base URL.
    :param session_id: The cursor-native session/conversation id.
    :param marker: The literal token to wait for (the per-turn user marker).
    :param timeout_ms: Max wait in milliseconds.
    """
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
def test_native_cursor_message_render_parity(
    page: Page,
    native_cursor_session: tuple[str, str],
) -> None:
    """Native Cursor renders parity with its TUI, both ways, with no dupes.

    Covers all three properties on a single (expensive to spin up) native
    session: composer parity (IN), a TUI-originated turn surfacing in the web UI
    (OUT), and no duplicate rendering across every turn. Mirrors
    ``test_native_codex_message_render_parity``.
    """
    base_url, session_id = native_cursor_session
    _log.info("native-cursor session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # The Terminal view proves cursor-agent actually booted in the session
    # terminal (the runner's cursor-native auto-launch) before we send anything.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Cursor TUI attached (terminal-view connected)")

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
        expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
            timeout=_NATIVE_TURN_TIMEOUT_MS
        )
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
    # Stay on the Terminal view until the forwarder has mirrored this turn —
    # leaving it tears down the xterm WS and can drop the just-pressed Enter
    # before cursor-agent's (slower-than-Codex) composer commits it.
    _wait_marker_in_transcript(base_url, session_id, tui_token, timeout_ms=_NATIVE_TURN_TIMEOUT_MS)

    # Back in Chat, the forwarder must have mirrored the TUI turn OUT as a user
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
