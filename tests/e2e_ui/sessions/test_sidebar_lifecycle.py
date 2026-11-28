"""Full-stack coverage for the sidebar's session-organization lifecycle."""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, Request, expect


def _row(page: Page, session_id: str) -> Locator:
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _section(page: Page, title: str) -> Locator:
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _set_title(base_url: str, session_id: str, title: str) -> None:
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    response.raise_for_status()


def _rename_from_row(page: Page, session_id: str, title: str) -> None:
    row = _row(page, session_id)
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()
    editor = page.get_by_test_id("rename-conversation-input")
    editor.fill(title)
    editor.press("Enter")


def _archive_from_row(page: Page, session_id: str) -> None:
    row = _row(page, session_id)
    expect(row).to_be_visible()
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()


def _wait_for_archived(base_url: str, session_id: str, expected: bool) -> None:
    deadline = time.monotonic() + 15.0
    actual: bool | None = None
    while time.monotonic() < deadline:
        response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if response.status_code == 200:
            actual = response.json()["archived"]
            if actual is expected:
                return
        time.sleep(0.25)
    raise AssertionError(f"session {session_id} should report archived={expected}, got {actual}")


def test_sidebar_session_organization_round_trip(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Pin, rename, archive, and undo through the real server."""
    base_url, session_a, session_b = seeded_session_pair
    initial_title = f"e2e-lifecycle-{uuid.uuid4().hex[:8]}"
    renamed_title = f"{initial_title}-renamed"
    _set_title(base_url, session_a, initial_title)
    _set_title(base_url, session_b, f"{initial_title}-sibling")

    stop_events: list[str] = []

    def record_stop(request: Request) -> None:
        if (
            request.method == "POST"
            and request.url.endswith("/events")
            and "stop_session" in (request.post_data or "")
        ):
            stop_events.append(request.url)

    page.on("request", record_stop)
    page.goto(f"{base_url}/c/{session_a}")

    row = _row(page, session_a)
    expect(row).to_be_visible()
    row.hover()
    row.get_by_test_id("quick-pin-conversation").click()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')).to_be_visible()

    _rename_from_row(page, session_a, renamed_title)
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_contain_text(renamed_title)

    page.reload()
    pinned_link = _section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')
    expect(pinned_link).to_contain_text(renamed_title)
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_a}", timeout=10.0)
    snapshot.raise_for_status()
    assert snapshot.json()["title"] == renamed_title

    _archive_from_row(page, session_a)
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_have_count(0)
    page.wait_for_url(f"{base_url}/", timeout=10_000)

    _archive_from_row(page, session_b)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_have_count(0)
    toast = page.get_by_test_id("archive-undo-toast-item")
    expect(toast).to_contain_text("Archived 2 sessions")
    _wait_for_archived(base_url, session_a, True)
    _wait_for_archived(base_url, session_b, True)
    # A client stop would serialize runner timeouts ahead of archive or race
    # the server teardown, which can orphan a host-spawned runner.
    assert stop_events == [], f"archive must not send stop_session, sent {stop_events}"

    toast.get_by_role("button", name="Undo").click()
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_have_count(1)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_have_count(1)
    _wait_for_archived(base_url, session_a, False)
    _wait_for_archived(base_url, session_b, False)

    restored = _section(page, "Sessions").locator(f'a[href="/c/{session_a}"]')
    expect(restored).to_contain_text(renamed_title)
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')).to_have_count(0)

    page.reload()
    restored = _section(page, "Sessions").locator(f'a[href="/c/{session_a}"]')
    expect(restored).to_contain_text(renamed_title, timeout=15_000)
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')).to_have_count(0)
