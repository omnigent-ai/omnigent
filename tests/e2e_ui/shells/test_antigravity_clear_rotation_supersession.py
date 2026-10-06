"""Antigravity ``/clear`` rotation must notify the superseded web conversation.

Typing ``/clear`` in the native ``agy`` TUI starts a fresh cascade, and the
antigravity-native reader rotates the Omnigent session onto a new conversation.
The old conversation must not be left stranded: a viewer of it auto-redirects
to the new chat, and the old transcript keeps an assistant notice linking it.

Run (no credentials)::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest \\
        tests/e2e_ui/shells/test_antigravity_clear_rotation_supersession.py \\
        --ui-skip-build
"""

from __future__ import annotations

import os
import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Browser, Locator, expect

from .test_antigravity_tmux_recovery import (  # noqa: F401 (fixtures used by name)
    _BLOCK_LOOPBACK_DIALS,
    AntigravitySession,
    antigravity_model,
    antigravity_session,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

_REDIRECT_TIMEOUT_MS = 30_000
_ROTATION_TIMEOUT_S = 120.0
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'


def _session_ids(base_url: str) -> set[str]:
    resp = httpx.get(
        f"{base_url}/v1/sessions", params={"limit": 100, "visibility": "all"}, timeout=15.0
    )
    resp.raise_for_status()
    return {item["id"] for item in resp.json().get("data", [])}


def _superseded_notice_items(
    base_url: str, old_session_id: str, new_session_id: str
) -> list[dict]:
    """Return the old conversation's persisted assistant items that link the new chat."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{old_session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return [
        item
        for item in resp.json().get("data", [])
        if item.get("type") == "message"
        and item.get("role") == "assistant"
        and f"/c/{new_session_id}" in str(item.get("content", ""))
    ]


def _wait_for_rotation_child(base_url: str, before_ids: set[str], *, timeout_s: float) -> str:
    """Block until the ``/clear`` rotation creates its replacement session."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        new_ids = _session_ids(base_url) - before_ids
        if new_ids:
            assert len(new_ids) == 1, f"expected exactly one rotation child, got {sorted(new_ids)}"
            return next(iter(new_ids))
        time.sleep(1.0)
    raise AssertionError(
        f"agy /clear never created a rotation child session within {timeout_s:.0f}s; "
        "the cascade rotation did not fire"
    )


def _wait_for_terminal_quiescent(
    terminal: Locator, *, baseline: str, settle_s: float = 1.0, timeout_s: float = 30.0
) -> None:
    """Wait until the agy TUI reacts to the last keypress and then stops redrawing.

    ``/clear`` swaps in a fresh cascade asynchronously; sending the next prompt
    before the swap settles can route it into the old cascade and skip rotation.
    """
    deadline = time.monotonic() + timeout_s
    reacted = False
    last_text = baseline
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.25)
        current = terminal.inner_text()
        if current != last_text:
            reacted, last_text, stable_since = True, current, time.monotonic()
        elif reacted and time.monotonic() - stable_since >= settle_s:
            return
    # The headless xterm does not always surface the /clear redraw as a text
    # change, so settle is best-effort; _wait_for_rotation_child is the
    # authoritative gate and fails clearly if the rotation never fires.


# pytest resolves the imported fixtures by parameter name, so the shadowing is intended.
def test_antigravity_clear_rotation_supersedes_old_conversation(
    request: pytest.FixtureRequest,
    browser: Browser,
    antigravity_session: AntigravitySession,  # noqa: F811
    antigravity_model: list[str] | None,  # noqa: F811
) -> None:
    """Typing ``/clear`` in the agy TUI redirects the viewer and annotates the old chat."""
    session = antigravity_session
    base_url, old_session_id = session.base_url, session.session_id

    # The session-scoped browser must launch before antigravity_model patches
    # HOME; the page is created only once the non-browser stack is up so a
    # recording starts on the journey rather than on boot.
    page = request.getfixturevalue("page")
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{base_url}/c/{old_session_id}?view=terminal")

    main_terminal = page.get_by_test_id("main-terminal-view")
    terminal = main_terminal.get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)

    # One ordinary turn so the old conversation is a live chat before it is superseded.
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_placeholder("Send a message…")
    composer.fill(f"Reply {token}. No tools.")
    page.get_by_role("button", name="Send", exact=True).click()
    reply = page.locator(_ASSISTANT_BUBBLE)
    expect(reply.filter(has_text=token)).to_have_count(1, timeout=180_000)
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)

    page.get_by_test_id("view-mode-terminal").click()
    expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)

    before_ids = _session_ids(base_url)

    # /clear mints a fresh cascade, but the reader only rotates once that
    # cascade is used, so the first prompt in the new conversation is the trigger.
    xterm_input = terminal.locator(".xterm-helper-textarea")
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type("/clear", delay=20)
    cleared_baseline = terminal.inner_text()
    page.keyboard.press("Enter")
    _wait_for_terminal_quiescent(terminal, baseline=cleared_baseline)

    follow_up = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    xterm_input.focus()
    page.keyboard.type(f"Reply {follow_up}. No tools.", delay=15)
    page.keyboard.press("Enter")

    new_session_id = _wait_for_rotation_child(base_url, before_ids, timeout_s=_ROTATION_TIMEOUT_S)

    # A viewer of the old conversation is redirected to the new chat...
    expect(page).to_have_url(
        re.compile(rf"/c/{re.escape(new_session_id)}(\?|#|$)"),
        timeout=_REDIRECT_TIMEOUT_MS,
    )

    # ...and the old conversation keeps a persisted notice linking the new chat.
    assert _superseded_notice_items(base_url, old_session_id, new_session_id), (
        f"old conversation {old_session_id} has no assistant notice linking /c/{new_session_id}"
    )
    page.goto(f"{base_url}/c/{old_session_id}?view=chat")
    notice = page.locator(_ASSISTANT_BUBBLE).filter(has_text="This conversation was ended by")
    expect(notice.first).to_be_visible(timeout=30_000)
    expect(notice.first).to_contain_text("Continue in the new chat")
