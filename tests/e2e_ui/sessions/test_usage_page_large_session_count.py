"""Playwright coverage for the Usage page when a user owns hundreds of priced sessions."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

SESSION_COUNT = 300
_SEED_WORKERS = 8
_USAGE_URL = re.compile(r"/v1/usage(\?.*)?$")


def _hello_world_agent_id(base_url: str) -> str:
    agents = httpx.get(f"{base_url}/v1/agents", timeout=30.0).json()["data"]
    return next(agent["id"] for agent in agents if agent["name"] == "hello_world")


def _seed_priced_session(
    client: httpx.Client,
    base_url: str,
    agent_id: str,
    index: int,
    created_ids: list[str],
    lock: Lock,
) -> None:
    """Create one session and record its spend the way native harness forwarders do."""
    created = client.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id, "title": f"Priced session {index + 1:03d}"},
        timeout=30.0,
    )
    created.raise_for_status()
    body = created.json()
    session_id = body.get("session_id") or body["id"]
    # Record the id before the usage POST so teardown deletes it even if a
    # later step fails partway through seeding.
    with lock:
        created_ids.append(session_id)
    usage = client.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "cumulative_cost_usd": round(0.01 * (index + 1), 4),
                "cumulative_input_tokens": 1_000 + index,
                "cumulative_output_tokens": 200,
                "model": "gpt-4o-mini",
            },
        },
        timeout=30.0,
    )
    usage.raise_for_status()


@pytest.fixture
def priced_sessions(live_server: str) -> Iterator[list[str]]:
    """Seed SESSION_COUNT priced sessions through the product API; delete them afterwards."""
    agent_id = _hello_world_agent_id(live_server)
    session_ids: list[str] = []
    lock = Lock()
    try:
        with httpx.Client() as client, ThreadPoolExecutor(max_workers=_SEED_WORKERS) as pool:
            list(
                pool.map(
                    lambda index: _seed_priced_session(
                        client, live_server, agent_id, index, session_ids, lock
                    ),
                    range(SESSION_COUNT),
                )
            )
        yield session_ids
    finally:
        with httpx.Client() as client, ThreadPoolExecutor(max_workers=_SEED_WORKERS) as pool:
            list(
                pool.map(
                    lambda session_id: _delete_session(client, live_server, session_id),
                    session_ids,
                )
            )


def _delete_session(client: httpx.Client, base_url: str, session_id: str) -> None:
    """Delete a seeded session; a failed cleanup must surface, not leak into other tests."""
    response = client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    if response.status_code != 404:
        response.raise_for_status()


def _advertise_usage_page(page: Page) -> None:
    """Pass the real /v1/info through with the usage_page release feature on."""

    def handle_info(route: Route) -> None:
        upstream = fetch_with_retry(route)
        body = upstream.json()
        body.setdefault("features", {})["usage_page"] = True
        route.fulfill(
            status=upstream.status, content_type="application/json", body=json.dumps(body)
        )

    page.route("**/v1/info", handle_info)


@pytest.mark.min_server_version("0.19.0")
def test_usage_page_pages_sessions_for_large_session_counts(
    request: pytest.FixtureRequest,
    live_server: str,
    priced_sessions: list[str],
) -> None:
    """Hundreds of priced sessions load as a bounded first page; Load More appends the next."""
    # The browser opens only after seeding so a recording starts on the journey.
    page: Page = request.getfixturevalue("page")
    _advertise_usage_page(page)
    page.goto(live_server)
    usage_nav = page.get_by_test_id("usage-nav")
    expect(usage_nav).to_be_visible(timeout=30_000)

    with page.expect_response(_USAGE_URL, timeout=60_000) as first_fetch:
        usage_nav.click()
    first_page = first_fetch.value
    assert first_page.ok, f"GET {first_page.url} -> {first_page.status}"
    first_body = first_page.json()
    first_sessions = first_body["sessions"]
    # The client requests a 50-row first page, so the response stays bounded no
    # matter how many sessions the account owns.
    assert len(first_sessions) == 50, (
        f"GET {first_page.url} returned {len(first_sessions)} sessions, expected the 50-row bound"
    )
    assert first_body["sessions_has_more"] is True
    cursor = first_body["sessions_last_id"]
    assert cursor == first_sessions[-1]["id"]

    # The sidebar's own session list paginates with a Load more button; only
    # the routed Usage page inside <main> is under test.
    usage_page = page.get_by_role("main")
    expect(usage_page.get_by_role("heading", name="Usage", exact=True)).to_be_visible(
        timeout=30_000
    )
    rows = usage_page.locator("table tbody tr")
    expect(rows).to_have_count(len(first_sessions), timeout=60_000)
    rows.last.scroll_into_view_if_needed()
    load_more = usage_page.get_by_role("button", name=re.compile("load more", re.I))
    expect(load_more).to_be_visible()

    with page.expect_response(_USAGE_URL, timeout=60_000) as next_fetch:
        load_more.click()
    assert f"after={cursor}" in next_fetch.value.url, (
        f"Load More requested {next_fetch.value.url} without the first page's cursor"
    )
    next_sessions = next_fetch.value.json()["sessions"]
    assert next_sessions, f"GET {next_fetch.value.url} returned an empty next page"
    expect(rows).to_have_count(len(first_sessions) + len(next_sessions), timeout=60_000)
