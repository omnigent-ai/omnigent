"""Full-stack coverage for the compaction indicator's lifecycle."""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"
_INDICATOR = '[data-testid="compacting-indicator"]'
_ELAPSED_RE = re.compile(r"\((\d+)s\)")


def _post_event(base_url: str, session_id: str, event_type: str, data: dict[str, str]) -> None:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
        timeout=10.0,
    )
    response.raise_for_status()


def _post_compaction_status(base_url: str, session_id: str) -> None:
    _post_event(base_url, session_id, "external_compaction_status", {"status": "in_progress"})


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


def test_compaction_indicator_clears_when_its_turn_settles(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A native turn that ends without its compaction terminal takes the spinner along."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=20_000)

    # Native forwarders bracket a turn with status edges that name it; the
    # compaction-completed edge in between never arrives.
    turn = {"response_id": "native_turn_compacting"}
    _post_event(base_url, session_id, "external_session_status", {"status": "running", **turn})
    _post_compaction_status(base_url, session_id)
    indicator = page.locator(_INDICATOR)
    expect(indicator).to_be_visible(timeout=10_000)

    _post_event(base_url, session_id, "external_session_status", {"status": "idle", **turn})
    expect(indicator).to_have_count(0, timeout=10_000)
