"""Full-stack coverage for active compaction state across a reload."""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"
_INDICATOR = '[data-testid="compacting-indicator"]'
_ELAPSED_RE = re.compile(r"\((\d+)s\)")


def _post_compaction_status(base_url: str, session_id: str) -> None:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_compaction_status", "data": {"status": "in_progress"}},
        timeout=10.0,
    )
    response.raise_for_status()


def _elapsed_seconds(page: Page) -> int:
    match = _ELAPSED_RE.search(page.locator(_INDICATOR).last.inner_text())
    return int(match.group(1)) if match else 0


def test_compaction_elapsed_time_survives_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The restored indicator remains anchored to the server's start time."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)

    _post_compaction_status(base_url, session_id)
    indicator = page.locator(_INDICATOR)
    expect(indicator).to_be_visible(timeout=10_000)
    page.wait_for_timeout(5_000)
    elapsed_before = _elapsed_seconds(page)
    assert elapsed_before >= 3

    page.reload()
    expect(composer).to_be_visible(timeout=20_000)
    _post_compaction_status(base_url, session_id)
    expect(indicator).to_be_visible(timeout=10_000)
    page.wait_for_timeout(1_500)

    elapsed_after = _elapsed_seconds(page)
    assert elapsed_after >= elapsed_before, (
        f"compaction elapsed time reset across reload: {elapsed_before}s -> {elapsed_after}s"
    )
