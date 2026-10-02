"""Long prompts reach a real agy TUI even when agy scrolls the draft in its composer.

A draft taller than agy's input box shows only its tail and hides the start behind an
``↑ N more lines`` row, so the bridge never sees the prompt's first line before pressing
Enter. These runs drive the server, runner, tmux and the real Antigravity CLI against a
local mock Gemini backend (no credentials); set ``OMNIGENT_E2E_ANTIGRAVITY=mock``.

Both journeys pin the agy pane to 80x24 before delivery so the paragraph wraps past the
composer's height regardless of any attached terminal: the ``posted`` journey delivers with
no browser attached, and the ``composer`` journey sends from the web chat composer whose
terminal attach would otherwise widen the pane and let the draft fit.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.harnesses.antigravity_native.bridge import (
    _AGY_SCROLL_OVERFLOW_RE,
    _agy_input_region,
    _format_pane_debug_tail,
    read_tmux_info,
)
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (  # noqa: F401 — fixtures reused
    AntigravitySession,
    _wait_until,
    antigravity_model,
    antigravity_session,
)
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

_IDLE_FOOTER = "? for shortcuts"
_DELIVERY_ERROR = "Could not deliver the turn to the agy TUI"
_AGY_READY_TIMEOUT_S = 120.0
_REPLY_TIMEOUT_S = 90.0


def _long_single_paragraph_prompt(token: str) -> str:
    """A single ~950-char paragraph agy renders verbatim and scrolls behind its overflow row.

    The length is deliberate: a shorter paragraph fits the 80x24 composer and delivers either
    way, and a much longer paste (past ~1,000 chars) collapses to a ``[Pasted text #N]``
    placeholder the gate already recognises. In between, agy hides the draft's first line behind
    an ``↑ N more lines`` row — the shape that scrolls the draft's first line out of view.
    The opening is unique so the first-line needle really disappears; the token stays at the
    end so a delivered turn shows up in the mock's reply.
    """
    return (
        "Please review the repository notes for the next release and write a short "
        "report for the team: summarise the purpose of last week's change to how "
        "messages are typed into the terminal in two or three plain-language sentences, "
        "list the files you believe are most relevant with one line each on why they "
        "matter, call out any assumptions the change makes about terminal size, line "
        "wrapping or paste handling and whether those assumptions are written down "
        "anywhere in the repository, note which tests cover the behaviour and whether "
        "they exercise a long single-paragraph prompt or only short messages, describe "
        "how a reviewer could tell a draft that was pasted but never submitted apart "
        "from one that was submitted and answered, and suggest two follow-up checks a "
        "reviewer could run by hand to double-check the behaviour before approving. This "
        "is a read-only task, so do not edit files or run commands that change state. "
        f"Reply {token}. No tools."
    )


def _capture_pane(session: AntigravitySession) -> str:
    result = session.tmux_command("capture-pane", "-p", "-t", session.pane()["tmux_target"])
    assert result.returncode == 0, result.stderr
    return result.stdout


def _pin_pane_geometry(session: AntigravitySession, cols: int = 80, rows: int = 24) -> None:
    """Pin the agy pane to a fixed size so the paragraph wraps past the composer's height.

    An attached browser terminal would otherwise widen the pane and let the draft fit; a
    manual window size holds the pane steady and ignores client resizes.
    """
    target = session.pane()["tmux_target"]
    manual = session.tmux_command("set-window-option", "-t", target, "window-size", "manual")
    assert manual.returncode == 0, manual.stderr
    resized = session.tmux_command("resize-window", "-t", target, "-x", str(cols), "-y", str(rows))
    assert resized.returncode == 0, resized.stderr


def _assert_prompt_overflows_composer(session: AntigravitySession, prompt: str) -> None:
    """Prove the live agy composer scrolls this prompt behind its ``↑ N more lines`` row.

    Pastes the prompt into the real composer without submitting, so a geometry or CLI
    change that let the paragraph fit without overflowing fails here instead of quietly
    routing delivery around the behaviour this suite guards.
    """
    target = session.pane()["tmux_target"]
    buffer_name = "e2e-overflow-probe"
    assert session.tmux_command("set-buffer", "-b", buffer_name, "--", prompt).returncode == 0
    paste = session.tmux_command("paste-buffer", "-p", "-d", "-b", buffer_name, "-t", target)
    assert paste.returncode == 0, paste.stderr

    def _overflow_rendered() -> bool:
        region = _agy_input_region(_capture_pane(session))
        return _AGY_SCROLL_OVERFLOW_RE.search(region) is not None

    _wait_until(
        _overflow_rendered,
        "agy did not scroll the long prompt behind an overflow row at this geometry",
        30,
    )
    # Clear the probe draft so the delivered-journey assertions see a clean composer.
    assert session.tmux_command("send-keys", "-t", target, "C-u").returncode == 0
    _wait_until(
        lambda: _composer_is_empty(session),
        "overflow probe draft was not cleared",
        10,
    )


def _composer_is_empty(session: AntigravitySession) -> bool:
    lines = _agy_input_region(_capture_pane(session)).splitlines()
    return bool(lines) and all(line.strip() in {"", ">"} for line in lines)


def _wait_for_agy_idle(session: AntigravitySession) -> None:
    _wait_until(
        lambda: read_tmux_info(session.bridge_dir) is not None,
        "runner did not advertise the agy tmux pane",
        _AGY_READY_TIMEOUT_S,
    )
    _wait_until(
        lambda: _IDLE_FOOTER in _capture_pane(session),
        "agy never rendered its idle composer footer",
        _AGY_READY_TIMEOUT_S,
    )


def _session_items(session: AntigravitySession) -> list[dict]:
    try:
        response = httpx.get(
            f"{session.base_url}/v1/sessions/{session.session_id}/items", timeout=10
        )
        response.raise_for_status()
    except httpx.TransportError:
        return []  # transient blip; the polling caller retries
    return response.json()["data"]


def _assistant_replied(items: list[dict], token: str) -> bool:
    return any(
        item.get("type") == "message"
        and item.get("role") == "assistant"
        and any(token in part.get("text", "") for part in item.get("content", []))
        for item in items
    )


def _delivery_error(items: list[dict]) -> str:
    return next(
        (
            item.get("message", "")
            for item in items
            if item.get("type") == "error" and _DELIVERY_ERROR in item.get("message", "")
        ),
        "",
    )


def _wait_for_reply(session: AntigravitySession, token: str) -> None:
    """Wait for the assistant's reply; fail with the composer contents if nothing was submitted."""
    deadline = time.monotonic() + _REPLY_TIMEOUT_S
    while time.monotonic() < deadline:
        items = _session_items(session)
        if _assistant_replied(items, token):
            break
        error = _delivery_error(items)
        assert not error, (
            f"the turn was not submitted: {error}\n--- agy composer ---\n"
            f"{_format_pane_debug_tail(_capture_pane(session))}"
        )
        time.sleep(1.0)
    else:
        raise AssertionError(
            f"no assistant reply within {_REPLY_TIMEOUT_S:.0f}s; "
            f"agy pane:\n{_format_pane_debug_tail(_capture_pane(session))}"
        )
    _wait_until(
        lambda: _composer_is_empty(session),
        "the submitted prompt stayed in agy's composer",
        30,
    )


def _open_chat(page: Page, session: AntigravitySession) -> None:
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{session.base_url}/c/{session.session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    page.get_by_test_id("view-mode-chat").click()


def _reply_bubble(page: Page, token: str) -> Locator:
    return page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=token
    )


def _show_terminal(page: Page) -> None:
    # Reveal the agy TUI and hold it on screen so the demo recording captures the
    # delivered prompt in the terminal, after chat-side delivery is already verified.
    page.get_by_test_id("view-mode-terminal").click()
    terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    page.wait_for_timeout(2_000)


def test_posted_long_single_paragraph_prompt_is_delivered(
    page: Page,
    antigravity_session: AntigravitySession,  # noqa: F811  (imported fixture)
) -> None:
    """A ~950-char paragraph posted with no client attached (80x24 pane) is delivered."""
    session = antigravity_session
    _wait_for_agy_idle(session)
    _pin_pane_geometry(session)
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    prompt = _long_single_paragraph_prompt(token)
    response = httpx.post(
        f"{session.base_url}/v1/sessions/{session.session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        },
        timeout=30,
    )
    response.raise_for_status()
    _wait_for_reply(session, token)
    _assert_prompt_overflows_composer(session, prompt)

    _open_chat(page, session)
    expect(_reply_bubble(page, token)).to_have_count(1, timeout=60_000)
    _show_terminal(page)


def test_composer_long_single_paragraph_prompt_is_delivered(
    page: Page,
    antigravity_session: AntigravitySession,  # noqa: F811  (imported fixture)
) -> None:
    """A ~950-char paragraph sent from the web chat composer is delivered and answered."""
    session = antigravity_session
    _wait_for_agy_idle(session)
    _open_chat(page, session)
    _pin_pane_geometry(session)
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    prompt = _long_single_paragraph_prompt(token)
    page.get_by_placeholder("Send a message…").fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()
    _wait_for_reply(session, token)
    _assert_prompt_overflows_composer(session, prompt)
    expect(_reply_bubble(page, token)).to_have_count(1, timeout=60_000)
    _show_terminal(page)
