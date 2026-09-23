"""E2E: failed chat submissions must stay recoverable and never resend blindly.

Three journeys around a failing ``POST /v1/sessions/<id>/events``:

1. A send whose failure lands after the user typed a newer draft must keep
   BOTH: the newer draft in the composer and the failed submission (text and
   attachment) recoverable on the page.
2. Two failed submissions must each remain recoverable — recovery must not
   funnel through a single composer slot where composing the next message
   destroys the only copy of the previous one.
3. A transport error after the POST reached the server does not mean the
   message was rejected. Retrying the recovered draft must not commit a
   duplicate user message or dispatch a second model turn.

Without retained failed submissions all three regress: the failed submission
is discarded when the composer is non-empty, only the last failure is
recoverable, and the retry re-posts a message the server already committed
and answered.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER_LABEL = "Message the agent"
_SEND_BUTTON = "Send"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


def _is_message_post(route: Route) -> bool:
    if route.request.method != "POST":
        return False
    try:
        parsed = json.loads(route.request.post_data or "")
    except ValueError:
        return False
    return parsed.get("type") == "message"


def _open_chat(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)


def test_delayed_failure_keeps_submission_and_newer_draft(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    held: list[Route] = []

    def handle(route: Route) -> None:
        if _is_message_post(route):
            held.append(route)
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/events", handle)
    _open_chat(page, base_url, session_id)
    composer = page.get_by_label(_COMPOSER_LABEL)

    attachment = tmp_path / "notes.txt"
    attachment.write_text("attachment payload\n")
    page.locator('form.chat-composer-form input[type="file"]').set_input_files(str(attachment))
    expect(page.get_by_text("notes.txt").first).to_be_visible()

    composer.fill("first message that will fail")
    page.get_by_role("button", name=_SEND_BUTTON, exact=True).click()
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "message POST was never issued"

    composer.fill("my newer unsent draft")
    held[0].abort("failed")
    page.wait_for_timeout(2_000)

    expect(composer).to_have_value("my newer unsent draft")
    expect(
        page.get_by_text("first message that will fail").first,
        "the failed submission's text must stay recoverable on the page",
    ).to_be_visible()
    expect(
        page.get_by_text("notes.txt").first,
        "the failed submission's attachment must stay recoverable on the page",
    ).to_be_visible()


def test_each_failed_submission_stays_recoverable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    aborted: list[str] = []

    def handle(route: Route) -> None:
        if _is_message_post(route):
            aborted.append(route.request.post_data or "")
            route.abort("failed")
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/events", handle)
    _open_chat(page, base_url, session_id)
    composer = page.get_by_label(_COMPOSER_LABEL)

    composer.fill("message A destined to fail")
    page.get_by_role("button", name=_SEND_BUTTON, exact=True).click()
    for _ in range(100):
        if aborted:
            break
        page.wait_for_timeout(100)
    assert aborted, "first message POST was never issued"
    page.wait_for_timeout(1_500)

    composer.fill("message B destined to fail")
    page.get_by_role("button", name=_SEND_BUTTON, exact=True).click()
    for _ in range(100):
        if len(aborted) >= 2:
            break
        page.wait_for_timeout(100)
    assert len(aborted) >= 2, "second message POST was never issued"
    page.wait_for_timeout(1_500)

    composer_value = composer.input_value()
    a_recoverable = (
        "message A destined to fail" in composer_value
        or page.get_by_text("message A destined to fail").count() > 0
    )
    b_recoverable = (
        "message B destined to fail" in composer_value
        or page.get_by_text("message B destined to fail").count() > 0
    )
    assert a_recoverable and b_recoverable, (
        "each failed submission must stay recoverable, got composer="
        f"{composer_value!r}, A on page={a_recoverable}, B on page={b_recoverable}"
    )


def test_uncertain_post_retry_does_not_duplicate_the_message(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "reply one"}, {"text": "reply two"}],
        key="uncertain-send-dup",
        match="please deduplicate me",
    )
    lost = [0]

    def handle(route: Route) -> None:
        if _is_message_post(route) and lost[0] == 0:
            lost[0] += 1
            # Deliver the POST to the server, then drop the response: the
            # server commits the message and starts the turn while the client
            # sees a transport error of unknown disposition.
            route.fetch()
            route.abort("failed")
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/events", handle)
    _open_chat(page, base_url, session_id)
    composer = page.get_by_label(_COMPOSER_LABEL)

    composer.fill("please deduplicate me")
    page.get_by_role("button", name=_SEND_BUTTON, exact=True).click()

    committed = page.locator(_USER_BUBBLE).filter(has_text="please deduplicate me")
    expect(committed.first).to_be_visible(timeout=15_000)
    # Let the first (server-side) turn finish so the retry click issues a fresh
    # POST rather than queueing behind a still-streaming turn — otherwise the
    # duplicate outcome would hinge on machine speed.
    expect(page.get_by_text("reply one").first).to_be_visible(timeout=15_000)

    # The uncertain send is offered back for retry: the message the server
    # already committed is restored into the composer. Poll rather than check
    # once so a slow render can't skip the retry and mask the duplicate.
    offered_retry = False
    for _ in range(40):
        if composer.input_value().strip() == "please deduplicate me":
            offered_retry = True
            break
        page.wait_for_timeout(250)

    # Retry only through what the UI offers; a fixed client may instead
    # reconcile and offer nothing, which is correct too.
    if offered_retry:
        page.get_by_role("button", name=_SEND_BUTTON, exact=True).click()
    page.wait_for_timeout(5_000)

    expect(
        committed,
        "retrying an uncertain send must not commit the message twice",
    ).to_have_count(1)
    expect(
        page.get_by_text("reply two"),
        "retrying an uncertain send must not dispatch a second model turn",
    ).to_have_count(0)
    expect(page.get_by_text(re.compile("reply one")).first).to_be_visible(timeout=15_000)
