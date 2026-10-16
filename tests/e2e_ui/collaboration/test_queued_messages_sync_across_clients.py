"""E2E: follow-ups queued in one client are visible in a second client on the same session.

Two independent browser contexts open the same ``/c/<id>`` URL (the desktop
app and a browser tab are two such clients). The first client starts a turn the
mock LLM holds open on its gate, then each client submits a follow-up while the
turn is running, so both follow-ups park in the queued-messages strip above the
composer. The queue belongs to the session, so each client's strip must list
both follow-ups in the same order, and once the turn ends they must drain in
that order with every message rendered once in both transcripts.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
import uuid
from collections.abc import Callable

import httpx
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.conftest import (
    configure_mock_llm,
    release_mock_gate,
    reset_mock_llm,
    wait_for_mock_gate,
)

_COMPOSER_LABEL = "Message the agent"
_QUEUED_LIST = "Queued messages"


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _release_gates(mock_url: str) -> None:
    """Release every held turn so teardown cannot leave the shared mock blocked."""
    deadline = time.monotonic() + 30.0
    while release_mock_gate(mock_url) and time.monotonic() < deadline:
        time.sleep(0.2)


def _user_bubbles(page: Page, known: list[str]) -> list[str]:
    """User bubbles in transcript order, each named by the known message it shows."""
    texts = page.locator('[data-testid="message-bubble"][data-role="user"]').all_inner_texts()
    return [next((m for m in known if m in text), text.strip()) for text in texts]


def _drain_turns_until(mock_url: str, done: Callable[[], bool], *, timeout_s: float) -> None:
    """Release the mock's gate whenever a turn blocks on it, until ``done``."""
    deadline = time.monotonic() + timeout_s
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError("the queued follow-ups did not drain in time")
        with contextlib.suppress(httpx.HTTPError):
            release_mock_gate(mock_url)
        time.sleep(0.5)


def test_queued_followups_are_shared_across_clients(
    browser: Browser,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    nonce = uuid.uuid4().hex[:8]
    first_followup = f"first-client follow-up {nonce}"
    second_followup = f"second-client follow-up {nonce}"
    # Several gated replies: background traffic embedding the first message
    # (title generation) can match the nonce and take a gate.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Done waiting.", "block": True}] * 4,
        key=f"queue-sync-gate-{nonce}",
        match=f"gate-{nonce}",
    )

    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    first_ctx = browser.new_context(record_video_dir=record_dir)
    second_ctx = browser.new_context(record_video_dir=record_dir)
    try:
        first = first_ctx.new_page()
        second = second_ctx.new_page()
        first.goto(f"{base_url}/c/{session_id}")
        second.goto(f"{base_url}/c/{session_id}")
        expect(first.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
        expect(second.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

        first_message = f"Please take your time with this one. gate-{nonce}"
        _send(first, first_message)
        wait_for_mock_gate(mock_llm_server_url)
        expect(
            second.locator(
                '[data-testid="message-bubble"][data-role="user"]', has_text=first_message
            )
        ).to_be_visible(timeout=30_000)

        _send(first, first_followup)
        first_strip = first.get_by_test_id("composer-queued-strip")
        second_strip = second.get_by_test_id("composer-queued-strip")
        expect(first_strip).to_contain_text(first_followup, timeout=15_000)
        expect(second_strip).to_contain_text(first_followup, timeout=15_000)

        _send(second, second_followup)
        expect(second_strip).to_contain_text(second_followup, timeout=15_000)
        expect(first_strip).to_contain_text(second_followup, timeout=15_000)

        expected_rows = [
            re.compile(re.escape(first_followup)),
            re.compile(re.escape(second_followup)),
        ]
        expect(first.get_by_role("list", name=_QUEUED_LIST).get_by_role("listitem")).to_have_text(
            expected_rows
        )
        expect(second.get_by_role("list", name=_QUEUED_LIST).get_by_role("listitem")).to_have_text(
            expected_rows
        )

        # Let the held turn end: the follow-ups drain one per turn in the order
        # both strips showed, and each client's live transcript lists every
        # message once, in that order.
        expected_bubbles = [first_message, first_followup, second_followup]
        _drain_turns_until(
            mock_llm_server_url,
            lambda: (
                _user_bubbles(first, expected_bubbles) == expected_bubbles
                and _user_bubbles(second, expected_bubbles) == expected_bubbles
                and first_strip.count() == 0
                and second_strip.count() == 0
            ),
            timeout_s=90.0,
        )
        expect(first_strip).to_have_count(0)
        expect(second_strip).to_have_count(0)
        assert _user_bubbles(first, expected_bubbles) == expected_bubbles
        assert _user_bubbles(second, expected_bubbles) == expected_bubbles
    finally:
        # Release the held turn and drop the gated queue so a follow-up turn that
        # flushes during teardown cannot block on the shared mock.
        with contextlib.suppress(httpx.HTTPError):
            _release_gates(mock_llm_server_url)
            reset_mock_llm(mock_llm_server_url)
        first_ctx.close()
        second_ctx.close()
