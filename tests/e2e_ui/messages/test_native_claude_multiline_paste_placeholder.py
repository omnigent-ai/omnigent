"""UI journey: multi-line web-composer messages stay readable in the Claude Code terminal.

The native bridge delivers a web message into Claude Code's input box as one
bracketed paste. Claude Code collapses a paste holding three or more line
breaks into a ``[Pasted text #N +X lines]`` placeholder, in its input box and
in its prompt history (Up arrow). A message with exactly two line breaks is
the boundary: a person can type it as-is, so it must arrive as two breaks,
not three. The Chat view shows the full text either way.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import _ASSISTANT, _USER, _WORKING, _ensure_chat_view
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _focus_tui,
    _open_terminal_view,
    _pane_text,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_PROMPT_GLYPH = "❯"
_TURN_TIMEOUT_MS = 90_000
_TUI_READY_TIMEOUT_S = 120.0

# Line templates per case; ``{n}`` is a per-message nonce. The one-line-break
# message is the control Claude Code keeps as text however it arrives; the
# two-line-break message collapses only if the bridge adds a third break.
_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("one-line-break", ("first line {n}", "second line {n}")),
    ("two-line-breaks", ("first line {n}", "", "third line {n}")),
)


def _history_path() -> Path:
    """Claude Code's prompt history, resolved the way Claude Code resolves it."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(configured) if configured else Path.home() / ".claude"
    return config_dir / "history.jsonl"


def _history_displays(nonce: str) -> list[str]:
    """Return the ``display`` text of every history entry mentioning *nonce*."""
    path = _history_path()
    if not path.exists():
        return []
    displays: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if nonce not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # Claude Code appends concurrently; a half-written line is not an entry yet.
            continue
        displays.append(str(entry.get("display", "<missing display>")))
    return displays


def _wait_history_displays(nonce: str, *, timeout_s: float) -> list[str]:
    """Poll Claude Code's history until an entry mentioning *nonce* appears."""
    deadline = time.monotonic() + timeout_s
    displays = _history_displays(nonce)
    while not displays and time.monotonic() < deadline:
        time.sleep(0.2)
        displays = _history_displays(nonce)
    return displays


def _input_line(pane: str) -> str:
    """Return the pane's last prompt row: Claude Code's input box."""
    rows = [row.strip() for row in pane.splitlines() if row.lstrip().startswith(_PROMPT_GLYPH)]
    return rows[-1] if rows else ""


def _wait_pane(
    base_url: str,
    session_id: str,
    predicate: Callable[[str], bool],
    *,
    timeout_s: float,
) -> str | None:
    """Poll the TUI pane until *predicate* accepts it; return that pane or ``None``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        if predicate(pane):
            return pane
        time.sleep(0.2)
    return None


def _send_multiline(page: Page, lines: tuple[str, ...]) -> None:
    """Type *lines* into the composer with Shift+Enter breaks and click Send."""
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    for index, line in enumerate(lines):
        if index:
            page.keyboard.press("Shift+Enter")
        if line:
            page.keyboard.type(line, delay=5)
    text = "\n".join(lines)
    assert composer.input_value() == text, (
        f"Shift+Enter typing produced {composer.input_value()!r} instead of {text!r}"
    )
    page.get_by_role("button", name="Send", exact=True).click()


def _send_case(
    page: Page,
    *,
    mock_llm_server_url: str,
    label: str,
    lines: tuple[str, ...],
    nonce: str,
    expected_user_bubbles: int,
) -> list[str]:
    """Send one multi-line message; return how Claude Code's history recorded it."""
    token = f"ast-{nonce}"
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)
    _ensure_chat_view(page)
    _send_multiline(page, lines)
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_USER)).to_have_count(expected_user_bubbles, timeout=30_000)
    bubble = page.locator(_USER, has_text=nonce).first
    expect(bubble).to_be_visible()
    bubble_text = bubble.inner_text()
    assert all(line in bubble_text for line in lines if line), (
        f"Chat view lost text for {label}: {lines!r} rendered as {bubble_text!r}"
    )
    displays = _wait_history_displays(nonce, timeout_s=10)
    _log.info("%s: Claude Code history recorded %r", label, displays)
    return displays


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
def test_native_claude_multiline_message_stays_readable_in_terminal(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A three-line web message is kept as text, not a paste placeholder, by Claude Code."""
    base_url, session_id = native_claude_mock_session
    reset_mock_llm(mock_llm_server_url)

    # Request the page after the session setup so a recording opens on the journey.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    assert _wait_pane(
        base_url, session_id, lambda pane: _PROMPT_GLYPH in pane, timeout_s=_TUI_READY_TIMEOUT_S
    ), "Claude Code composer never rendered"

    texts: dict[str, str] = {}
    first_lines: dict[str, str] = {}
    history: dict[str, list[str]] = {}
    for index, (label, templates) in enumerate(_CASES, start=1):
        nonce = uuid.uuid4().hex[:8]
        lines = tuple(template.format(n=nonce) for template in templates)
        texts[label] = "\n".join(lines)
        first_lines[label] = lines[0]
        history[label] = _send_case(
            page,
            mock_llm_server_url=mock_llm_server_url,
            label=label,
            lines=lines,
            nonce=nonce,
            expected_user_bubbles=index,
        )

    # Up arrow recalls the most recent prompt: the two-line-break message.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _focus_tui(page)
    page.keyboard.press("ArrowUp")
    recalled = _wait_pane(
        base_url,
        session_id,
        lambda pane: _input_line(pane) not in ("", _PROMPT_GLYPH),
        timeout_s=10,
    )
    recalled_line = _input_line(recalled or "")
    _log.info("ArrowUp recalled %r", recalled_line)
    # Leave the recalled prompt on screen so a recording ends on it.
    page.wait_for_timeout(2_500)

    problems: list[str] = []
    if history["one-line-break"] != [texts["one-line-break"]]:
        problems.append(f"two-line control recorded in history as {history['one-line-break']}")
    if history["two-line-breaks"] != [texts["two-line-breaks"]]:
        problems.append(
            "three-line message (two line breaks) recorded in Claude Code history as "
            f"{history['two-line-breaks']} instead of its text"
        )
    if recalled is None:
        problems.append("ArrowUp did not change the input box within the timeout")
    elif not recalled_line.endswith(first_lines["two-line-breaks"]):
        problems.append(f"ArrowUp recalled {recalled_line!r} instead of the message text")
    assert not problems, "\n".join(problems)
