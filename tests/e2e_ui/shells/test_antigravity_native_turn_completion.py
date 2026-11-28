"""An agy turn stays in progress until agy's reply arrives.

Drives the real ``agy`` CLI through the server, runner and tmux against a mock
Gemini that holds its reply, so agy is still working after the prompt lands::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest --ui-skip-build -v \
        tests/e2e_ui/shells/test_antigravity_native_turn_completion.py
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from omnigent_client import OmnigentClient
from omnigent_client._sessions_chat import SessionsChat, _assistant_text_from_output_item
from playwright.sync_api import Page, expect

from omnigent.harnesses.antigravity_native.bridge import _AGY_IDLE_MARKER, read_tmux_info
from omnigent.server.schemas import OutputItemDoneEvent, ServerStreamEvent
from tests.e2e_ui.shells.test_antigravity_native_cold_start_mirror import (
    _pane_text,
    _playwright_browsers_outside_isolated_home,  # noqa: F401 - autouse fixture reused by name
)
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (
    AntigravitySession,
    _wait_until,
    antigravity_model,  # noqa: F401 - fixture reused by name
    antigravity_session,  # noqa: F401 - fixture reused by name
)
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

_HOLD_S = 15
_IDLE_PLACEHOLDER = "Send a message…"
_FOLLOW_UP_PLACEHOLDER = "Send a follow-up (queued) — Esc to stop"


def _task(token: str) -> str:
    return f"Reply {token} after {_HOLD_S} seconds. No tools."


def _wait_for_agy_prompt(session: AntigravitySession) -> None:
    _wait_until(lambda: read_tmux_info(session.bridge_dir) is not None, "agy pane never launched")
    _wait_until(lambda: _AGY_IDLE_MARKER in _pane_text(session), "agy TUI never became ready", 120)


def test_composer_stays_in_follow_up_mode_while_agy_works(
    request: pytest.FixtureRequest,
    antigravity_session: AntigravitySession,  # noqa: F811
    antigravity_model: list[str] | None,  # noqa: F811
) -> None:
    session = antigravity_session
    _wait_until(lambda: read_tmux_info(session.bridge_dir) is not None, "agy pane never launched")
    page = request.getfixturevalue("page")
    try:
        _drive_composer_journey(page, session, antigravity_model)
    finally:
        page.context.close()


def _drive_composer_journey(
    page: Page, session: AntigravitySession, model_requests: list[str] | None
) -> None:
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
    terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)
    _wait_for_agy_prompt(session)
    page.wait_for_timeout(1_500)

    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_label("Message the agent")
    composer.fill(_task(token))
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=token
    )
    page.get_by_role("button", name="Send", exact=True).click()
    sent = time.monotonic()
    expect(composer).to_have_attribute("placeholder", _FOLLOW_UP_PLACEHOLDER)

    while True:
        placeholder = composer.get_attribute("placeholder")
        if reply.count():
            break
        elapsed = round(time.monotonic() - sent, 2)
        assert placeholder == _FOLLOW_UP_PLACEHOLDER, (
            f"composer showed {placeholder!r} at +{elapsed}s, before agy's reply"
        )
        assert elapsed < 90, "agy's reply never appeared in Chat"
        page.wait_for_timeout(250)
    replied_at = round(time.monotonic() - sent, 2)

    if model_requests is not None:
        assert token in model_requests, "agy did not request this reply from the mock model"
        assert replied_at >= _HOLD_S, f"agy replied at +{replied_at}s, inside the mock's hold"
    expect(composer).to_have_attribute("placeholder", _IDLE_PLACEHOLDER, timeout=30_000)


def test_sdk_send_returns_after_agy_replies(
    antigravity_session: AntigravitySession,  # noqa: F811
) -> None:
    session = antigravity_session
    _wait_for_agy_prompt(session)
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"

    async def send() -> list[ServerStreamEvent]:
        async with OmnigentClient(base_url=session.base_url) as client:
            chat = SessionsChat(
                namespace=client.sessions,
                files_uploader=None,
                files_getter=None,
                session=await client.sessions.get(session.session_id),
            )
            return [event async for event in chat.send(_task(token))]

    sent = time.monotonic()
    # Playwright's loop owns this thread, so the SDK client runs on its own.
    with ThreadPoolExecutor(1) as pool:
        events = pool.submit(asyncio.run, send()).result()
    returned_at = round(time.monotonic() - sent, 2)
    reply = [
        text
        for event in events
        if isinstance(event, OutputItemDoneEvent)
        for text in _assistant_text_from_output_item(event.item)
    ]
    assert token in "".join(reply), (
        f"chat.send returned at +{returned_at}s without agy's reply; "
        f"last event: {events[-1].type if events else None}"
    )
