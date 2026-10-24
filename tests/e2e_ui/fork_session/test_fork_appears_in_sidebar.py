"""Browser e2e: a forked session is listed in the sidebar while the session list
lags and the live-updates stream misses the fork, like a created session.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from playwright.sync_api import Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

_MARKER = "rambutan-fork-sidebar-marker"
_ROW_TIMEOUT_MS = 20_000
_STALE_LIST_S = 30.0
_FORK_TITLE_PREFIX = "Fork of "
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# List GETs only: ``/v1/sessions?…`` but not ``/v1/sessions/{id}`` or the fork POST.
_LIST_RE = re.compile(r"/v1/sessions\?[^/]*$")


@dataclass
class ForkVisibilityFaults:
    """Hide new forks from list reads and live-updates frames for a lag window."""

    stale_list_s: float = _STALE_LIST_S
    fork_ids: set[str] = field(default_factory=set)
    stale_until: float = 0.0
    lag_started_at: float | None = None
    list_responses: list[dict[str, Any]] = field(default_factory=list)
    dropped_push_items: list[dict[str, Any]] = field(default_factory=list)
    forwarded_frame_types: list[str] = field(default_factory=list)
    sockets: list[str] = field(default_factory=list)

    def lagging(self) -> bool:
        return time.monotonic() < self.stale_until

    def hides(self, item: dict[str, Any]) -> bool:
        # Title match: a push or list read can race the fork response; this test forks once.
        title = str(item.get("title") or "")
        return self.lagging() and (
            item.get("id") in self.fork_ids or title.startswith(_FORK_TITLE_PREFIX)
        )

    def diagnostics(self) -> str:
        return (
            f"lists={self.list_responses} dropped={self.dropped_push_items} "
            f"frames={self.forwarded_frame_types}"
        )

    def elapsed(self) -> float | None:
        if self.lag_started_at is None:
            return None
        return round(time.monotonic() - self.lag_started_at, 1)

    def install(self, page: Page) -> None:
        page.route(re.compile(r"/v1/sessions/[^/]+/fork$"), self._record_fork)
        page.route(_LIST_RE, self._lag_list)
        page.route_web_socket(re.compile(r"/v1/sessions/updates"), self._filter_updates)

    def _record_fork(self, route: Route) -> None:
        self.lag_started_at = time.monotonic()
        self.stale_until = self.lag_started_at + self.stale_list_s
        response = fetch_with_retry(route)
        if response.ok:
            self.fork_ids.add(response.json()["id"])
        route.fulfill(response=response)

    def _lag_list(self, route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        if not response.ok:
            route.fulfill(response=response)
            return
        body: dict[str, Any] = response.json()
        removed = [row["id"] for row in body.get("data", []) if self.hides(row)]
        self.list_responses.append(
            {"t": self.elapsed(), "url": route.request.url, "removed_fork_ids": removed}
        )
        if not removed:
            route.fulfill(response=response)
            return
        body["data"] = [row for row in body["data"] if row["id"] not in removed]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _filter_updates(self, ws: WebSocketRoute) -> None:
        self.sockets.append(ws.url)
        server = ws.connect_to_server()
        ws.on_message(server.send)

        def to_page(message: str | bytes) -> None:
            if isinstance(message, bytes):
                ws.send(message)
                return
            frame = json.loads(message)
            frame_type = str(frame.get("type"))
            self.forwarded_frame_types.append(frame_type)
            items = frame.get("items")
            if not isinstance(items, list):
                ws.send(message)
                return
            kept = [item for item in items if not self.hides(item)]
            for item in items:
                if item not in kept:
                    self.dropped_push_items.append(
                        {"t": self.elapsed(), "frame": frame_type, "id": item.get("id")}
                    )
            if len(kept) == len(items):
                ws.send(message)
            elif kept or frame_type == "snapshot":
                ws.send(json.dumps({**frame, "items": kept}))

        server.on_message(to_page)


def sidebar_row(page: Page, session_id: str):
    return page.locator(f'a[href="/c/{session_id}"]')


def seed_turn_from_composer(page: Page, base_url: str, source_id: str, marker: str) -> None:
    """Send one marked message so the chat shows a committed exchange to fork from."""
    page.goto(f"{base_url}/c/{source_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"Reply with one short word. Marker: {marker}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(sidebar_row(page, source_id)).to_be_visible()


def fork_from_first_response(page: Page, source_id: str) -> str:
    """Click the first reply's Fork action, submit Clone, and return the fork id."""
    first_assistant = page.locator(_ASSISTANT).first
    expect(first_assistant).to_be_visible(timeout=30_000)
    first_assistant.hover()
    first_assistant.get_by_test_id("fork-from-response").click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone")
    submit.click()
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(source_id)})(conv_)?[0-9a-f]+"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    return page.url.rsplit("/c/", 1)[1].split("?", 1)[0]


def test_fork_row_while_list_lags_and_push_misses_fork(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The fork row appears without a caught-up list or push, and stays through the lag."""
    base_url, source_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url, [{"text": "OK"}], key="fork-sidebar-seed", match=_MARKER
    )
    faults = ForkVisibilityFaults()

    page: Page = request.getfixturevalue("page")
    faults.install(page)
    seed_turn_from_composer(page, base_url, source_id, _MARKER)
    assert faults.sockets, "the SPA never opened the session-updates socket"

    fork_id = fork_from_first_response(page, source_id)
    assert fork_id in faults.fork_ids, (fork_id, faults.fork_ids)
    row = sidebar_row(page, fork_id)
    expect(row).to_be_visible(timeout=_ROW_TIMEOUT_MS)

    gaps: list[float | None] = []
    while faults.lagging():
        page.wait_for_timeout(1_000)
        if row.count() == 0 or not row.first.is_visible():
            gaps.append(faults.elapsed())
    assert any(r["removed_fork_ids"] for r in faults.list_responses), faults.diagnostics()
    assert gaps == [], (
        f"fork row disappeared at {gaps}s while the list lagged; {faults.diagnostics()}"
    )

    page.reload()
    expect(row).to_be_visible(timeout=_ROW_TIMEOUT_MS)
