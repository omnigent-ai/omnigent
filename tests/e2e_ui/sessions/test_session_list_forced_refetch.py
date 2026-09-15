"""E2E: user activity must not force top-level session-list refetches.

The deployment's top-level ``GET /v1/sessions`` is served by unified
search, so every forced ``["conversations"]`` refetch is backend search
load that scales with user activity. Live updates already flow over the
``WS /v1/sessions/updates`` in-place cache patch; these journeys assert
the SPA relies on it instead of re-fetching the whole list.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER_LABEL = "Message the agent"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'


def _track_top_level_list_requests(page: Page) -> list[tuple[float, str]]:
    """Record every top-level ``GET /v1/sessions`` (list) the page issues."""
    log: list[tuple[float, str]] = []
    t0 = time.monotonic()

    def on_request(request: Request) -> None:
        if request.method != "GET":
            return
        url = urlparse(request.url)
        if url.path != "/v1/sessions":
            return
        log.append((round(time.monotonic() - t0, 2), url.query))

    page.on("request", on_request)
    return log


def _wait_quiet(page: Page, log: list, quiet_ms: int = 3_000, timeout_s: float = 45.0) -> None:
    """Wait until no top-level list request lands for ``quiet_ms``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        count = len(log)
        page.wait_for_timeout(quiet_ms)
        if len(log) == count:
            return
    raise AssertionError(f"top-level GET /v1/sessions never went quiet: {log}")


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label(_COMPOSER_LABEL)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_message_send_does_not_force_session_list_refetch(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "warm-up reply"}, {"text": "measured reply"}],
    )
    log = _track_top_level_list_requests(page)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    # Warm-up turn absorbs one-time list triggers (e.g. title auto-gen)
    # so the measured window isolates the send itself.
    _send(page, "warm-up ping")
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    _wait_quiet(page, log)

    baseline = len(log)
    _send(page, "measured ping")
    expect(page.locator(_ASSISTANT)).to_have_count(2, timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    # Longer than the provider's invalidation debounce, far shorter than
    # the 60s connected reconcile poll, so only send-driven fetches land.
    page.wait_for_timeout(2_500)

    forced = log[baseline:]
    assert forced == [], (
        f"sending one message forced {len(forced)} top-level GET /v1/sessions "
        f"refetch(es) (search queries on the unified-search deployment): {forced}"
    )


def test_ws_pushed_change_converges_without_list_refetch(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    base_url, active_id, other_id = seeded_session_pair
    log = _track_top_level_list_requests(page)

    page.goto(f"{base_url}/c/{active_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    other_row = page.locator(f'a[href="/c/{other_id}"]')
    expect(other_row).to_be_visible(timeout=30_000)
    _wait_quiet(page, log)

    baseline = len(log)
    new_title = "pushed title update"
    httpx.patch(
        f"{base_url}/v1/sessions/{other_id}", json={"title": new_title}, timeout=10.0
    ).raise_for_status()

    # The WS frame's in-place cache patch must surface the new title on
    # its own; a full list refetch is the bug, not the delivery vehicle.
    expect(other_row).to_contain_text(new_title, timeout=15_000)
    page.wait_for_timeout(2_500)

    forced = log[baseline:]
    assert forced == [], (
        f"a pushed title change forced {len(forced)} top-level GET /v1/sessions "
        f"refetch(es) instead of converging via the in-place patch: {forced}"
    )
