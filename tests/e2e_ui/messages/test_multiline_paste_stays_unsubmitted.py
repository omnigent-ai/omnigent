"""E2E: pasting multiline text must not submit it to the agent by itself.

Two surfaces accept a paste of text containing newlines:

- The embedded terminal pane of a terminal-first (native TUI) session. The
  TUI (Claude Code) enables bracketed paste, so a multi-line paste must land
  in its input box as one block; only an explicit Enter submits it. When the
  browser xterm never learns bracketed paste is active, it sends the pasted
  newlines as raw carriage returns and the TUI submits the first line the
  moment the paste arrives — text the user never sent races off to the agent.
- The chat composer. A paste must insert into the draft; only the configured
  send gesture submits.

Both journeys paste through the browser's real clipboard + paste gesture, not
synthetic value assignment, so the xterm/composer paste pipelines are the ones
exercised.
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _TERMINAL_VIEW,
    _XTERM_INPUT,
    _open_terminal_view,
    _pane_text,
    _wait_terminal_connected,
)

_COMPOSER_LABEL = "Message the agent"

# A pane row is an input-box border when it carries a long run of box-drawing
# dashes; the idle TUI composer is the region between the last two such rows.
_BORDER_RUN = re.compile("─{10,}")

# How long the paste gets to surface in the TUI pane (browser -> WebSocket ->
# tmux -> Claude Code repaint), and how long after that a premature submission
# would have repainted the pane.
_PASTE_SURFACE_TIMEOUT_S = 20.0
_SUBMIT_SETTLE_S = 3.0


def _pane_lines(pane: str) -> list[str]:
    """Split a pane capture into rows, dropping trailing blank padding."""
    lines = pane.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _input_box_region(pane: str) -> tuple[list[str], list[str]] | None:
    """Split *pane* into (rows above the input box, rows inside it).

    The idle Claude Code composer is bordered above and below by full-width
    box-drawing rules; everything above its top border is transcript/echo
    territory, where only *submitted* prompts render.

    :param pane: The pane's visible text.
    :returns: ``(above, inside)`` line lists, or ``None`` while the pane has
        no complete input box to split on (mid-repaint).
    """
    lines = _pane_lines(pane)
    borders = [i for i, line in enumerate(lines) if _BORDER_RUN.search(line)]
    if len(borders) < 2:
        return None
    top, bottom = borders[-2], borders[-1]
    return lines[:top], lines[top + 1 : bottom]


def _paste_visible(rows: list[str], markers: tuple[str, ...]) -> bool:
    """Whether the pasted block shows in *rows* — literally or collapsed.

    Claude Code may render a large paste as a ``[Pasted text …]`` placeholder
    instead of the literal lines; both count as the paste having arrived.
    """
    joined = "\n".join(rows)
    return all(marker in joined for marker in markers) or "Pasted text" in joined


@pytest.mark.nightly
@pytest.mark.timeout(300)
@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="requires the claude CLI and tmux",
)
def test_tui_multiline_paste_stays_unsubmitted(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A multi-line paste into the TUI pane must wait for Enter, not self-send."""
    base_url, session_id = native_claude_mock_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # A wrongly-submitted turn should resolve fast against the mock LLM rather
    # than leave the TUI mid-request while the pane is inspected.
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "paste-turn-token")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "paste-turn-token")

    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.wait_for_timeout(2_000)

    # Prove keystrokes reach the TUI composer before trusting the paste result;
    # a paste that never lands would otherwise pass vacuously.
    nonce = uuid.uuid4().hex[:6]
    sanity = f"sanity{nonce}"
    page.keyboard.type(sanity, delay=30)
    deadline = time.monotonic() + _PASTE_SURFACE_TIMEOUT_S
    while sanity not in _pane_text(base_url, session_id):
        assert time.monotonic() < deadline, "typed keys never reached the TUI pane"
        page.wait_for_timeout(500)
    for _ in range(len(sanity)):
        page.keyboard.press("Backspace")
    page.wait_for_timeout(500)

    # The real user journey: put a two-line block on the clipboard and paste it
    # with the browser's paste gesture (plain Ctrl+V is the terminal's literal
    # ^V byte, so terminals paste via Ctrl+Shift+V / Cmd+V).
    first = f"pasteblock-first-{nonce}"
    second = f"pasteblock-second-{nonce}"
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("([a, b]) => navigator.clipboard.writeText(a + '\\n' + b)", [first, second])
    xterm_input.press("Control+Shift+V")

    # Wait for the paste to surface in the pane at all, then give a premature
    # submission time to repaint before judging.
    deadline = time.monotonic() + _PASTE_SURFACE_TIMEOUT_S
    while not _paste_visible(_pane_lines(_pane_text(base_url, session_id)), (first, second)):
        assert time.monotonic() < deadline, "the paste never surfaced in the TUI pane"
        page.wait_for_timeout(500)
    page.wait_for_timeout(int(_SUBMIT_SETTLE_S * 1000))

    pane = _pane_text(base_url, session_id)
    region = _input_box_region(pane)
    deadline = time.monotonic() + _PASTE_SURFACE_TIMEOUT_S
    while region is None:
        assert time.monotonic() < deadline, f"no input box found in pane:\n{pane}"
        page.wait_for_timeout(500)
        pane = _pane_text(base_url, session_id)
        region = _input_box_region(pane)
    above, inside = region

    # The regression: with the paste treated as raw newlines, Claude Code
    # submits the first line immediately — it shows up echoed above the input
    # box as a sent prompt, with only the second line left in the box.
    assert not any(first in line for line in above), (
        "pasting a multi-line block submitted its first line to the agent "
        f"without Enter; pane:\n{pane}"
    )
    assert _paste_visible(inside, (first,)), (
        f"pasted first line is not in the TUI input box; pane:\n{pane}"
    )
    assert _paste_visible(inside, (second,)), (
        f"pasted second line is not in the TUI input box; pane:\n{pane}"
    )


def test_composer_multiline_paste_stays_in_composer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A multi-line paste into the chat composer inserts a draft, never sends."""
    base_url, session_id = seeded_session
    posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if isinstance(body, dict) and body.get("type") == "message":
            posts.append(str(body))

    page.on("request", record)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate(
        "navigator.clipboard.writeText('composer paste line one\\ncomposer paste line two')"
    )
    composer.press("Control+V")

    expect(composer).to_have_value("composer paste line one\ncomposer paste line two")
    page.wait_for_timeout(1_000)
    assert posts == [], f"pasting into the composer sent a message: {posts}"
