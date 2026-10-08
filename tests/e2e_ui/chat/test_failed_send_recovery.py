"""E2E: a failed web send stays in the transcript, retryable, beside newer drafts.

The failure is injected at the message POST; the attachment upload stays real.
A send whose POST reached the server but lost its response must not be re-offered.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_FAILED_SEND = '[data-testid="failed-send-message"]'
_FILE_INPUT = 'input[type="file"][accept*="image/"]'

_ATTACHMENT_NAME = "notes.txt"
_DELAYED_PROMPT = "first message that will fail"
_NEWER_DRAFT = "my newer unsent draft"
_PROMPT_A = "message A destined to fail"
_PROMPT_B = "message B destined to fail"
_DEDUPE_PROMPT = "please deduplicate me"
_DEDUPE_REPLY_ONE = "reply one"
_DEDUPE_REPLY_TWO = "reply two"

_TURN_TIMEOUT_MS = 30_000
_SETTLE_TIMEOUT_MS = 15_000


def _events_url(session_id: str) -> str:
    return f"**/v1/sessions/{session_id}/events"


def _is_message_post(route: Route) -> bool:
    if route.request.method != "POST":
        return False
    try:
        return json.loads(route.request.post_data or "").get("type") == "message"
    except (ValueError, AttributeError):
        return False


def _hold_message_post(page: Page, session_id: str) -> list[Route]:
    """Park the next message POST unanswered; the caller aborts it later."""
    held: list[Route] = []

    def _handle(route: Route) -> None:
        if held or not _is_message_post(route):
            route.continue_()
            return
        held.append(route)

    page.route(_events_url(session_id), _handle)
    return held


def _fail_every_message_post(page: Page, session_id: str) -> list[str]:
    failed: list[str] = []

    def _handle(route: Route) -> None:
        if not _is_message_post(route):
            route.continue_()
            return
        failed.append(route.request.post_data or "")
        route.abort("failed")

    page.route(_events_url(session_id), _handle)
    return failed


def _deliver_then_drop_response(page: Page, session_id: str, prompt: str) -> list[int]:
    """Let the first POST carrying ``prompt`` reach the server, then fail it client-side."""
    dropped = [0]

    def _handle(route: Route) -> None:
        if (
            dropped[0] > 0
            or not _is_message_post(route)
            or prompt not in (route.request.post_data or "")
        ):
            route.continue_()
            return
        # Count the drop only after the fetch reaches the server, so a failed
        # injection surfaces here instead of a confusing later assertion.
        route.fetch()
        dropped[0] += 1
        route.abort("internetdisconnected")

    page.route(_events_url(session_id), _handle)
    return dropped


def _open_session(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    return composer


def _attach(page: Page, tmp_path: Path, name: str) -> None:
    sample = tmp_path / name
    sample.write_text("repro notes line 1\nrepro notes line 2\n")
    page.locator(_FILE_INPUT).set_input_files(str(sample))
    expect(page.get_by_role("button", name=f"Remove {name}")).to_be_visible(timeout=10_000)


def _send(page: Page, composer: Locator, text: str) -> None:
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_until(page: Page, predicate: Callable[[], bool], timeout_ms: int) -> bool:
    for _ in range(max(1, timeout_ms // 100)):
        if predicate():
            return True
        page.wait_for_timeout(100)
    return predicate()


def _hold_for_viewer(page: Page) -> None:
    """Keep a demonstrated state on screen long enough to read when recording."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(2_500)


def _expect_optimistic_bubble_rolled_back(page: Page, text: str) -> None:
    expect(page.locator(_USER_BUBBLE).filter(has_text=text)).to_have_count(
        0, timeout=_SETTLE_TIMEOUT_MS
    )


def _committed_user_messages(base_url: str, session_id: str, text: str) -> int:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
    resp.raise_for_status()
    count = 0
    for item in resp.json()["data"]:
        data = item.get("data") or {}
        role = item.get("role") or data.get("role")
        content = item.get("content") or data.get("content") or []
        parts = content if isinstance(content, list) else [{"text": str(content)}]
        joined = " ".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        if item.get("type") == "message" and role == "user" and text in joined:
            count += 1
    return count


def test_delayed_failure_keeps_failed_message_beside_newer_draft(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """A send that fails after a newer draft was typed is retained and retryable."""
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    held = _hold_message_post(page, session_id)

    composer = _open_session(page, base_url, session_id)
    _attach(page, tmp_path, _ATTACHMENT_NAME)
    _send(page, composer, _DELAYED_PROMPT)
    expect(page.locator(_USER_BUBBLE).filter(has_text=_DELAYED_PROMPT)).to_be_visible(
        timeout=10_000
    )
    assert _wait_until(page, lambda: bool(held), _SETTLE_TIMEOUT_MS), (
        "the message POST was never intercepted; the delayed failure was not injected"
    )

    composer.fill(_NEWER_DRAFT)
    expect(composer).to_have_value(_NEWER_DRAFT)
    page.wait_for_timeout(1_000)

    held[0].abort("failed")
    _expect_optimistic_bubble_rolled_back(page, _DELAYED_PROMPT)

    expect(composer).to_have_value(_NEWER_DRAFT)
    expect(
        page.get_by_text(_DELAYED_PROMPT, exact=False).first,
        "the failed message is no longer shown anywhere on the page",
    ).to_be_visible(timeout=_SETTLE_TIMEOUT_MS)
    failed = page.locator(_FAILED_SEND).filter(has_text=_DELAYED_PROMPT)
    expect(failed).to_have_count(1)
    expect(failed).to_have_attribute("data-delivery-status", "not_sent")
    expect(failed).to_contain_text(_ATTACHMENT_NAME)
    expect(failed).to_contain_text("Failed to send")
    _hold_for_viewer(page)

    # Retrying the retained message delivers it and leaves the newer draft alone.
    failed.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator(_USER_BUBBLE).filter(has_text=_DELAYED_PROMPT)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    expect(page.locator(_FAILED_SEND)).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
    expect(composer).to_have_value(_NEWER_DRAFT)
    _hold_for_viewer(page)


def test_each_failed_send_is_retained_independently(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Two consecutive failed sends both remain on the page, each with its own retry."""
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    failed = _fail_every_message_post(page, session_id)

    composer = _open_session(page, base_url, session_id)
    _send(page, composer, _PROMPT_A)
    _expect_optimistic_bubble_rolled_back(page, _PROMPT_A)
    assert _wait_until(page, lambda: len(failed) == 1, _SETTLE_TIMEOUT_MS), (
        "the first message POST was not failed"
    )

    _send(page, composer, _PROMPT_B)
    _expect_optimistic_bubble_rolled_back(page, _PROMPT_B)
    assert _wait_until(page, lambda: len(failed) == 2, _SETTLE_TIMEOUT_MS), (
        "the second message POST was not failed"
    )

    for prompt in (_PROMPT_A, _PROMPT_B):
        expect(
            page.get_by_text(prompt, exact=False).first,
            f"{prompt!r} is no longer recoverable anywhere on the page",
        ).to_be_visible(timeout=_SETTLE_TIMEOUT_MS)
    cards = page.locator(_FAILED_SEND)
    expect(cards).to_have_count(2)
    expect(cards.nth(0)).to_contain_text(_PROMPT_A)
    expect(cards.nth(1)).to_contain_text(_PROMPT_B)
    expect(cards.get_by_role("button", name="Retry", exact=True)).to_have_count(2)
    expect(composer).to_have_value("")
    _hold_for_viewer(page)


def test_unacknowledged_send_is_not_reoffered_and_not_duplicated(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A send whose POST response was lost is neither re-offered nor run twice."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _DEDUPE_REPLY_ONE}, {"text": _DEDUPE_REPLY_TWO}],
        key="failed-send-recovery-dedupe",
        match=_DEDUPE_PROMPT,
    )
    page: Page = request.getfixturevalue("page")
    dropped = _deliver_then_drop_response(page, session_id, _DEDUPE_PROMPT)

    composer = _open_session(page, base_url, session_id)
    _send(page, composer, _DEDUPE_PROMPT)
    expect(page.locator(_ASSISTANT_BUBBLE).filter(has_text=_DEDUPE_REPLY_ONE)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    assert dropped[0] == 1, (
        "the send POST was never intercepted; the lost response was not injected"
    )
    page.wait_for_timeout(3_000)

    # Delivery was acknowledged over the stream before the POST response dropped,
    # so nothing may be re-offered (composer, Retry/Check card) or resent.
    committed = _committed_user_messages(base_url, session_id, _DEDUPE_PROMPT)
    user_bubbles = page.locator(_USER_BUBBLE).filter(has_text=_DEDUPE_PROMPT).count()
    second_turn = page.locator(_ASSISTANT_BUBBLE).filter(has_text=_DEDUPE_REPLY_TWO).count()
    assert composer.input_value() != _DEDUPE_PROMPT, (
        "the delivered message was handed back to the composer for a blind resend"
    )
    expect(page.locator(_FAILED_SEND).filter(has_text=_DEDUPE_PROMPT)).to_have_count(0)
    assert committed == 1 and user_bubbles == 1 and second_turn == 0, (
        f"duplicate dispatch: committed={committed}, bubbles={user_bubbles}, "
        f"second_turn={second_turn}"
    )
