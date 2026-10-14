"""Sidebar recovery after the fronting edge denies every ``/v1/**`` request with
HTTP 403 (a revoked workspace entitlement) and access is then restored."""

from __future__ import annotations

import json
import time

import pytest
from playwright.sync_api import Browser, Page, Response, Route, WebSocketRoute, expect
from playwright.sync_api import Error as PlaywrightError

EDGE_MESSAGE = "You do not have permission to access this resource."
_EDGE_BODY = json.dumps({"error_code": "PERMISSION_DENIED", "message": EDGE_MESSAGE})
# Covers the disconnected-stream safety poll plus React Query's retry backoff.
_REFETCH_BUDGET_S = 150.0


def _install_edge_denial(page: Page) -> dict[str, bool]:
    outage = {"active": False}

    def deny_http(route: Route) -> None:
        if not outage["active"]:
            route.fallback()
            return
        route.fulfill(status=403, content_type="application/json", body=_EDGE_BODY)

    def deny_ws(ws: WebSocketRoute) -> None:
        if outage["active"]:
            ws.close(code=1008, reason="forbidden")
            return
        ws.connect_to_server()

    page.route("**/v1/**", deny_http)
    page.route_web_socket("**/v1/sessions/updates*", deny_ws)
    return outage


def _session_row(page: Page, session_id: str):
    return page.locator(f'a[href*="/c/{session_id}"]').first


def _mine_list_rows(resp: Response) -> int | None:
    """Row count of a successful unpinned ``visibility=mine`` list response, else None."""
    url = resp.url
    if "/v1/sessions?" not in url or "visibility=mine" not in url or "pinned=true" in url:
        return None
    if resp.status != 200:
        return None
    try:
        return len(resp.json().get("data", []))
    except (ValueError, PlaywrightError):
        return None


@pytest.mark.timeout(420)
def test_sidebar_lists_sessions_again_after_edge_denial_ends(
    browser: Browser, custom_agent_session: tuple[str, str]
) -> None:
    base_url, session_id = custom_agent_session
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    refetched_rows: list[int] = []
    restored = {"active": False}

    def on_response(resp: Response) -> None:
        rows = _mine_list_rows(resp)
        if rows is not None and restored["active"]:
            refetched_rows.append(rows)

    page.on("response", on_response)
    try:
        outage = _install_edge_denial(page)

        page.goto(f"{base_url}/", wait_until="domcontentloaded")
        expect(_session_row(page, session_id)).to_be_visible(timeout=60_000)
        page.wait_for_timeout(2_000)

        outage["active"] = True
        page.reload(wait_until="domcontentloaded")
        denial = page.get_by_role("status").filter(has_text=f"Failed to load: {EDGE_MESSAGE}")
        expect(denial).to_be_visible(timeout=45_000)
        expect(page.get_by_role("button", name="Retry")).to_be_visible()
        expect(_session_row(page, session_id)).to_have_count(0)
        page.wait_for_timeout(3_000)

        outage["active"] = False
        restored["active"] = True
        deadline = time.monotonic() + _REFETCH_BUDGET_S
        while time.monotonic() < deadline and not refetched_rows:
            page.wait_for_timeout(1_000)
        assert refetched_rows, "the sidebar never refetched the session list after access returned"

        # The app's own refetch succeeded, so the row must come back without a reload.
        row = _session_row(page, session_id)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and row.count() == 0:
            page.wait_for_timeout(500)
        shown = (
            "denied"
            if denial.count()
            else "No sessions"
            if page.get_by_text("No sessions", exact=True).count()
            else "something else"
        )
        assert row.count() > 0, (
            f"after access returned the list refetch succeeded with {refetched_rows[0]} row(s), "
            f"but the sidebar shows {shown!r} until the page is reloaded"
        )
    finally:
        context.close()
