"""Slow or failed subtree usage must not prevent opening a conversation."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.chat.test_working_indicator_snapshot_reconcile import (
    _install_heartbeat_only_stream,
)
from tests.e2e_ui.conftest import fetch_with_retry, seed_committed_turn


def _publish_usage(base_url: str, session_id: str, cost: float | None, model: str) -> None:
    """Persist and broadcast native usage through the real events endpoint."""
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_usage",
            "data": {
                **({"cumulative_cost_usd": cost} if cost is not None else {}),
                "cumulative_input_tokens": 100,
                "model": model,
            },
        },
        timeout=10,
    )
    response.raise_for_status()


def _usage_panel(page: Page) -> Locator:
    """Open the user-facing usage popover without toggling an already-open one."""
    panel = page.get_by_test_id("agent-info-panel")
    trigger = page.get_by_test_id("agent-info-trigger")
    if trigger.get_attribute("aria-expanded") != "true":
        trigger.focus()
        trigger.press("Enter")
    expect(panel).to_be_visible()
    return panel


def _flush_browser_updates(page: Page) -> None:
    """Let an already-finished HTTP read and its React render complete."""
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


@pytest.mark.min_server_version("0.15.0")
@pytest.mark.parametrize("usage_status", [200, 503], ids=["delayed-success", "failure"])
def test_session_opens_before_subtree_usage(
    page: Page,
    seeded_session: tuple[str, str],
    usage_status: int,
) -> None:
    """The composer works while usage waits, and unknown cost never becomes zero."""
    base_url, session_id = seeded_session
    _publish_usage(base_url, session_id, 1.0, "parent-model")
    child = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_acp_subagent_start",
            "data": {"subagent_id": "usage-child", "title": "Usage child"},
        },
        timeout=10,
    )
    child.raise_for_status()
    _publish_usage(base_url, child.json()["child_session_id"], 2.5, "child-model")
    seed_committed_turn(session_id, prompt="Previous question", reply="History loads before usage")
    usage_url = f"{base_url}/v1/sessions/{session_id}/usage"
    pending_routes: list[Route] = []

    def hold_usage(route: Route) -> None:
        pending_routes.append(route)

    page.route(usage_url, hold_usage)
    with page.expect_request(usage_url, timeout=30_000):
        page.goto(f"{base_url}/c/{session_id}", wait_until="domcontentloaded")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_editable(timeout=30_000)
    expect(page.get_by_text("History loads before usage", exact=True)).to_be_visible()
    composer.fill("An unsent draft while usage is loading.")
    expect(composer).to_have_value("An unsent draft while usage is loading.")
    assert len(pending_routes) == 1

    trigger = page.get_by_test_id("agent-info-trigger")
    trigger.focus()
    trigger.press("Enter")
    panel = page.get_by_test_id("agent-info-panel")
    expect(panel).to_be_visible()
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_count(0)
    expect(panel.get_by_test_id("agent-info-usage-by-model")).to_have_count(0)

    upstream = fetch_with_retry(pending_routes[0])
    assert upstream.status == 200, upstream.text()
    assert upstream.json()["total_cost_usd"] == 3.5
    with page.expect_response(usage_url) as response:
        if usage_status == 200:
            pending_routes[0].fulfill(response=upstream)
        else:
            pending_routes[0].fulfill(
                status=usage_status,
                json={"error": {"code": "internal_error", "message": "Usage unavailable"}},
            )
    assert response.value.status == usage_status

    if usage_status == 200:
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$3.50")
        breakdown = panel.get_by_test_id("agent-info-usage-by-model")
        breakdown.locator("summary").press("Enter")
        expect(breakdown.get_by_test_id("agent-info-model-parent-model")).to_contain_text("$1.00")
        expect(breakdown.get_by_test_id("agent-info-model-child-model")).to_contain_text("$2.50")
    else:
        page.keyboard.press("Escape")
        composer.fill("Still editable after the usage request failed.")
        expect(composer).to_have_value("Still editable after the usage request failed.")
        trigger.focus()
        trigger.press("Enter")
        expect(panel).to_be_visible()
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_count(0)
        expect(panel.get_by_test_id("agent-info-usage-by-model")).to_have_count(0)


@pytest.mark.min_server_version("0.15.0")
@pytest.mark.parametrize("cost", [None, 0.0], ids=["unpriced", "priced-zero"])
def test_deferred_usage_keeps_unpriced_tokens_distinct_from_zero_cost(
    page: Page, seeded_session: tuple[str, str], cost: float | None
) -> None:
    """Real token-only usage remains visible without inventing or hiding a price."""
    base_url, session_id = seeded_session
    _publish_usage(base_url, session_id, cost, "unpriced-model")
    with page.expect_response(f"{base_url}/v1/sessions/{session_id}/usage") as usage:
        page.goto(f"{base_url}/c/{session_id}")
    assert usage.value.ok
    assert usage.value.json()["total_cost_usd"] == cost
    panel = _usage_panel(page)
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    breakdown.locator("summary").press("Enter")
    model = breakdown.get_by_test_id("agent-info-model-unpriced-model")
    expect(model).to_contain_text(re.compile(r"Input\s*100(?!\d)"))
    if cost is None:
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_count(0)
        expect(model).not_to_contain_text("$")
    else:
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$0.00")
        expect(model).to_contain_text("$0.00")


@pytest.mark.min_server_version("0.15.0")
def test_late_usage_response_cannot_replace_live_cost_or_model_breakdown(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A real native usage event wins over a previously captured HTTP result."""
    base_url, session_id = seeded_session
    _publish_usage(base_url, session_id, 1.0, "initial-model")
    usage_url = f"{base_url}/v1/sessions/{session_id}/usage"
    pending: list[Route] = []
    page.route(usage_url, lambda route: pending.append(route))
    with page.expect_response(lambda r: urlparse(r.url).path.endswith("/stream")):
        with page.expect_request(usage_url):
            page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_editable()
    panel = _usage_panel(page)
    stale = fetch_with_retry(pending[0])
    assert stale.status == 200, stale.text()
    assert stale.json()["total_cost_usd"] == 1.0

    _publish_usage(base_url, session_id, 4.0, "live-model")
    cost = panel.get_by_test_id("agent-info-session-cost")
    expect(cost).to_have_text("$4.00")
    panel.get_by_test_id("agent-info-usage-by-model").locator("summary").press("Enter")
    expect(panel.get_by_test_id("agent-info-model-live-model")).to_contain_text("$3.00")

    with page.expect_response(usage_url) as released:
        pending[0].fulfill(response=stale)
    released.value.finished()
    _flush_browser_updates(page)
    expect(cost).to_have_text("$4.00")
    expect(panel.get_by_test_id("agent-info-model-live-model")).to_contain_text("$3.00")
    assert len(pending) == 1


@pytest.mark.min_server_version("0.15.0")
def test_late_usage_response_stays_with_its_background_conversation(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """Switching A→B→A cannot redirect A's delayed usage into B's popover."""
    base_url, session_a, session_b = seeded_session_pair
    _publish_usage(base_url, session_a, 1.0, "model-a")
    _publish_usage(base_url, session_b, 9.0, "model-b")
    usage_a = f"{base_url}/v1/sessions/{session_a}/usage"
    pending: list[Route] = []
    page.route(usage_a, lambda route: pending.append(route))
    with page.expect_request(usage_a):
        page.goto(f"{base_url}/c/{session_a}")
    expect(page.get_by_placeholder("Send a message…")).to_be_editable()
    captured = fetch_with_retry(pending[0])
    assert captured.status == 200, captured.text()

    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}")
    # Finish the composer's navigation autofocus before opening the popover.
    expect(page.get_by_placeholder("Send a message…")).to_be_focused()
    panel = _usage_panel(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$9.00")
    with page.expect_response(usage_a) as released:
        pending[0].fulfill(response=captured)
    released.value.finished()
    _flush_browser_updates(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$9.00")

    page.keyboard.press("Escape")
    expect(panel).not_to_be_visible()
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}")
    expect(page.get_by_placeholder("Send a message…")).to_be_focused()
    panel = _usage_panel(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$1.00")
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    if breakdown.get_attribute("open") is None:
        breakdown.locator("summary").press("Enter")
    expect(breakdown.get_by_test_id("agent-info-model-model-a")).to_contain_text("$1.00")
    expect(breakdown.get_by_test_id("agent-info-model-model-b")).to_have_count(0)
    # Any reconnect refresh stayed held: A's cost came from the background read.
    for refresh in pending[1:]:
        refresh.fulfill(response=captured)


@pytest.mark.min_server_version("0.15.0")
def test_reconnect_hydrates_missed_usage_without_duplicate_pending_reads(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Missed live usage is recovered independently through one reconnect read."""
    base_url, session_id = seeded_session
    _publish_usage(base_url, session_id, 1.0, "session-model")
    usage_url = f"{base_url}/v1/sessions/{session_id}/usage"
    stream_path = f"/v1/sessions/{session_id}/stream"
    streams: list[Route] = []
    pending_usage: list[Route] = []
    page.expose_function("usageHeldStreamCount", lambda: len(streams))
    page.route(f"**{stream_path}*", lambda route: streams.append(route))
    with page.expect_request(lambda r: urlparse(r.url).path == stream_path):
        page.goto(f"{base_url}/c/{session_id}")
    panel = _usage_panel(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$1.00")

    with page.expect_request(lambda r: urlparse(r.url).path == stream_path):
        streams[0].fulfill(status=200, content_type="text/event-stream", body=": connected\n\n")
    page.wait_for_function("async () => await window.usageHeldStreamCount() >= 2")
    # Both streams are intercepted, so this broadcast lands in the offline gap.
    _publish_usage(base_url, session_id, 4.0, "session-model")
    page.route(usage_url, lambda route: pending_usage.append(route))
    with page.expect_request(lambda r: urlparse(r.url).path == stream_path):
        with page.expect_request(usage_url):
            streams[1].fulfill(
                status=200, content_type="text/event-stream", body=": connected\n\n"
            )
    page.wait_for_function("async () => await window.usageHeldStreamCount() >= 3")
    assert len(pending_usage) == 1

    with page.expect_response(
        lambda r: urlparse(r.url).path == f"/v1/sessions/{session_id}"
    ) as metadata:
        streams[2].continue_()
    metadata.value.finished()
    _flush_browser_updates(page)
    assert len(pending_usage) == 1, "reconnects must share the pending usage request"
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$1.00")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_editable()
    page.keyboard.press("Escape")
    composer.fill("A draft while reconnect usage is pending")
    panel = _usage_panel(page)

    upstream = fetch_with_retry(pending_usage[0])
    assert upstream.status == 200, upstream.text()
    assert upstream.json()["total_cost_usd"] == 4.0
    pending_usage[0].fulfill(response=upstream)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$4.00")
    expect(composer).to_have_value("A draft while reconnect usage is pending")


@pytest.mark.min_server_version("0.15.0")
def test_periodic_refresh_recovers_missed_usage_on_a_healthy_stream(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A missed usage event recovers without reconnecting or blocking the chat."""
    base_url, session_id = seeded_session
    _publish_usage(base_url, session_id, 1.0, "session-model")
    usage_url = f"{base_url}/v1/sessions/{session_id}/usage"
    pending_usage: list[Route] = []
    page.expose_function("periodicUsageReadCount", lambda: len(pending_usage))
    page.clock.install()
    _install_heartbeat_only_stream(page, session_id)
    with page.expect_response(usage_url) as initial_usage:
        page.goto(f"{base_url}/c/{session_id}")
    assert initial_usage.value.ok
    page.wait_for_function("window.__statusGapHeartbeats > 0")
    panel = _usage_panel(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$1.00")

    # The persisted usage changes, but this connected tab sees only heartbeats.
    page.route(usage_url, lambda route: pending_usage.append(route))
    _publish_usage(base_url, session_id, 4.0, "session-model")
    page.clock.run_for("00:50")
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$1.00")
    assert not pending_usage
    assert page.evaluate("window.__statusGapStreamOpens") == 1

    with page.expect_request(usage_url):
        page.clock.run_for("00:10")
    page.wait_for_function("async () => await window.periodicUsageReadCount() === 1")
    page.keyboard.press("Escape")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_editable()
    composer.fill("An unsent draft while periodic usage is pending")

    # Another status refresh must finish without awaiting or duplicating usage.
    with page.expect_response(
        lambda r: urlparse(r.url).path == f"/v1/sessions/{session_id}"
    ) as metadata:
        page.clock.run_for("01:00")
    assert metadata.value.ok
    assert metadata.value.json()["usage_included"] is False
    metadata.value.finished()
    _flush_browser_updates(page)
    assert len(pending_usage) == 1
    assert page.evaluate("window.__statusGapStreamOpens") == 1
    assert page.evaluate("window.__statusGapHeartbeats") >= 12

    upstream = fetch_with_retry(pending_usage[0])
    assert upstream.status == 200, upstream.text()
    assert upstream.json()["total_cost_usd"] == 4.0
    pending_usage[0].fulfill(response=upstream)
    panel = _usage_panel(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$4.00")
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    breakdown.locator("summary").press("Enter")
    expect(breakdown.get_by_test_id("agent-info-model-session-model")).to_contain_text("$4.00")
    expect(composer).to_have_value("An unsent draft while periodic usage is pending")
    assert page.evaluate("window.__statusGapStreamOpens") == 1


@pytest.mark.compat_smoke
def test_legacy_snapshot_usage_needs_no_separate_endpoint(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """An older snapshot supplies usage without the marker or a new endpoint."""
    base_url, session_id = seeded_session
    _publish_usage(base_url, session_id, 2.75, "legacy-model")
    session_url = f"{base_url}/v1/sessions/{session_id}"
    unsupported: list[str] = []

    def legacy_snapshot(route: Route) -> None:
        response = route.fetch(url=f"{session_url}?include_items=false&include_liveness=false")
        assert response.status == 200, response.text()
        body = response.json()
        body.pop("usage_included", None)
        route.fulfill(response=response, body=json.dumps(body))

    def unsupported_usage(route: Route) -> None:
        unsupported.append(route.request.url)
        route.fulfill(status=404, json={"detail": "Not Found"})

    page.route(f"{session_url}?*", legacy_snapshot)
    page.route(f"{session_url}/usage", unsupported_usage)
    page.goto(f"{base_url}/c/{session_id}")
    panel = _usage_panel(page)
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$2.75")
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    breakdown.locator("summary").press("Enter")
    expect(breakdown.get_by_test_id("agent-info-model-legacy-model")).to_contain_text("$2.75")
    expect(page.get_by_placeholder("Send a message…")).to_be_editable()
    _flush_browser_updates(page)
    assert not unsupported, "legacy snapshots must not trigger an unsupported /usage request"
