"""Latest-message errors use the error headline's red in the sidebar icon."""

from __future__ import annotations

import re
from typing import Literal

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.entities import ErrorData, NewConversationItem
from tests.e2e_ui.conftest import seed_committed_items, seed_committed_turn

_ERROR_MESSAGE = "API Error: Request rejected (429): Exceeded input tokens per minute rate limit."
_ERROR_RESPONSE_ID = "resp_sidebar_error"


def _row(page: Page, session_id: str) -> Locator:
    """Locate a sidebar row by its session link."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _error_badge(row: Locator) -> Locator:
    """Locate the error state in the row's shared trailing indicator slot."""
    return row.locator('[data-testid="session-state-badge"][data-state="error"]')


def _seed_notice(session_id: str) -> None:
    """Write a neutral notice without changing the session's idle status."""
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="error",
                response_id=_ERROR_RESPONSE_ID,
                data=ErrorData(
                    source="execution",
                    code="codex_thread_reset",
                    message="Codex could not load this session's saved transcript.",
                    level="info",
                ),
            ),
        ],
    )


def _publish_status(
    base_url: str,
    session_id: str,
    status: Literal["running", "idle", "failed"],
    *,
    response_id: str = _ERROR_RESPONSE_ID,
) -> None:
    """Send a native harness status edge without running a real model."""
    data = {"status": status, "response_id": response_id}
    if status == "failed":
        data["output"] = _ERROR_MESSAGE
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    response.raise_for_status()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_failed_session_matches_headline_red_and_survives_reading(
    page: Page,
    seeded_session: tuple[str, str],
    theme: Literal["light", "dark"],
) -> None:
    """The 14px CircleAlert keeps its error color and outranks the unread dot."""
    base_url, session_id = seeded_session
    page.emulate_media(color_scheme=theme)
    seed_committed_turn(
        session_id,
        prompt="Please continue.",
        reply=_ERROR_MESSAGE,
        response_id=_ERROR_RESPONSE_ID,
    )
    _publish_status(base_url, session_id, "failed")

    page.goto(base_url)
    row = _row(page, session_id)
    badge = _error_badge(row)
    expect(badge).to_be_visible(timeout=30_000)
    expect(badge).to_have_attribute("aria-label", "Latest message is an error")

    link = row.locator(f'a[href="/c/{session_id}"]')
    link.click()
    page.mouse.move(0, 0)
    headline = page.get_by_test_id("error-headline")
    expect(headline).to_be_visible(timeout=15_000)
    expect(badge).to_be_visible()
    expect(link).not_to_contain_text("(unread)")

    if theme == "dark":
        expect(page.locator("html")).to_have_class(re.compile(r"\bdark\b"))
    else:
        expect(page.locator("html")).not_to_have_class(re.compile(r"\bdark\b"))
    icon = badge.locator("svg.lucide-circle-alert")
    headline_color = headline.evaluate("element => getComputedStyle(element).color")
    expect(icon).to_have_css("color", headline_color)
    expect(icon).to_have_css("stroke", headline_color)
    expect(icon).to_have_css("width", "14px")
    expect(icon).to_have_css("height", "14px")

    link.hover()
    expect(page.get_by_test_id("session-tooltip-content")).to_contain_text(
        "Latest message is an error"
    )
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("mark-unread-conversation").click()
    page.mouse.move(0, 0)
    expect(link).to_contain_text("(unread)")
    expect(badge).to_be_visible()
    expect(row.locator('[data-state="unseen"]')).to_have_count(0)

    page.goto(base_url)
    expect(badge).to_be_visible(timeout=15_000)
    link.click()
    page.mouse.move(0, 0)
    expect(headline).to_be_visible(timeout=15_000)
    expect(link).not_to_contain_text("(unread)")
    expect(badge).to_be_visible()

    page.reload()
    expect(badge).to_be_visible(timeout=15_000)
    expect(icon).to_have_css("color", headline_color)


def test_live_native_failure_flags_an_unopened_session(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A native turn's failed status reaches the sidebar without opening it."""
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Please continue.",
        reply="Ready for the next task.",
        response_id=_ERROR_RESPONSE_ID,
    )
    page.goto(base_url)
    row = _row(page, session_id)
    expect(row).to_be_visible(timeout=30_000)
    expect(_error_badge(row)).to_have_count(0)

    _publish_status(base_url, session_id, "running")
    expect(row.locator('[data-state="running"]')).to_be_visible(timeout=15_000)
    _publish_status(base_url, session_id, "failed")
    expect(_error_badge(row)).to_be_visible(timeout=15_000)

    row.locator(f'a[href="/c/{session_id}"]').click()
    page.mouse.move(0, 0)
    headline = page.get_by_test_id("error-headline")
    expect(headline).to_be_visible(timeout=15_000)
    headline_color = headline.evaluate("element => getComputedStyle(element).color")
    expect(_error_badge(row).locator("svg")).to_have_css("color", headline_color)

    page.reload()
    expect(_error_badge(row)).to_be_visible(timeout=15_000)
    expect(headline).to_be_visible(timeout=15_000)


@pytest.mark.parametrize("recovery_status", ["running", "idle"])
def test_recovered_status_clears_the_session_error(
    page: Page,
    seeded_session: tuple[str, str],
    recovery_status: Literal["running", "idle"],
) -> None:
    """Resumed work clears the badge and a later idle status keeps it cleared."""
    base_url, session_id = seeded_session
    _publish_status(base_url, session_id, "failed")
    page.goto(base_url)
    row = _row(page, session_id)
    expect(_error_badge(row)).to_be_visible(timeout=30_000)

    recovery_response_id = "resp_sidebar_recovery"
    _publish_status(base_url, session_id, "running", response_id=recovery_response_id)
    expect(row.locator('[data-state="running"]')).to_be_visible(timeout=15_000)
    expect(_error_badge(row)).to_have_count(0, timeout=15_000)
    seed_committed_turn(
        session_id,
        prompt="Please try again.",
        reply="Recovered successfully.",
        response_id=recovery_response_id,
    )
    if recovery_status == "idle":
        _publish_status(base_url, session_id, "idle", response_id=recovery_response_id)
        expect(row.locator('[data-state="running"]')).to_have_count(0, timeout=15_000)

    page.reload()
    expect(row).to_be_visible(timeout=15_000)
    expect(_error_badge(row)).to_have_count(0)
    if recovery_status == "running":
        expect(row.locator('[data-state="running"]')).to_be_visible(timeout=15_000)
    else:
        expect(row.locator('[data-state="running"]')).to_have_count(0)


def test_neutral_notice_does_not_flag_the_session_as_an_error(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An info-level transcript item does not flag an idle session as failed."""
    base_url, session_id = seeded_session
    _seed_notice(session_id)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator('[data-testid="error-pill"][data-level="info"]')).to_be_visible(
        timeout=15_000
    )
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_error_badge(row)).to_have_count(0)


def test_idle_native_error_is_visible_before_opening_and_after_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Native API rejection text needs no failed status or last_task_error."""
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Please continue.",
        reply=_ERROR_MESSAGE,
        response_id=_ERROR_RESPONSE_ID,
    )
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}?include_items=false", timeout=10.0)
    snapshot.raise_for_status()
    assert snapshot.json()["status"] == "idle"
    assert snapshot.json().get("last_task_error") is None

    page.goto(base_url)
    row = _row(page, session_id)
    expect(_error_badge(row)).to_be_visible(timeout=30_000)
    row.locator(f'a[href="/c/{session_id}"]').click()
    page.mouse.move(0, 0)
    expect(page.get_by_text(_ERROR_MESSAGE, exact=True)).to_be_visible(timeout=15_000)
    expect(_error_badge(row)).to_be_visible()

    page.reload()
    expect(_error_badge(row)).to_be_visible(timeout=15_000)

    # A newer successful exchange replaces the error; read state does not.
    seed_committed_turn(
        session_id,
        prompt="Please try again.",
        reply="Recovered successfully.",
        response_id="resp_sidebar_recovered",
    )
    page.reload()
    expect(page.get_by_text("Recovered successfully.", exact=True)).to_be_visible(timeout=15_000)
    expect(_error_badge(row)).to_have_count(0, timeout=15_000)


def test_idle_structured_error_flags_an_unopened_session(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A persisted error item also survives a native session settling to idle."""
    base_url, session_id = seeded_session
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="error",
                response_id=_ERROR_RESPONSE_ID,
                data=ErrorData(
                    source="execution", code="rate_limit_exceeded", message=_ERROR_MESSAGE
                ),
            )
        ],
    )
    page.goto(base_url)
    row = _row(page, session_id)
    expect(_error_badge(row)).to_be_visible(timeout=30_000)
    row.locator(f'a[href="/c/{session_id}"]').click()
    page.mouse.move(0, 0)
    headline = page.get_by_test_id("error-headline")
    expect(headline).to_be_visible(timeout=15_000)
    expect(_error_badge(row).locator("svg")).to_have_css(
        "color", headline.evaluate("element => getComputedStyle(element).color")
    )
