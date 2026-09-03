"""E2E coverage for event-driven Canvas session-summary refreshes."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.sync_api import Page, Request, Route, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANVAS_DIST = _REPO_ROOT / "extensions" / "canvas" / "src" / "omnigent_canvas" / "dist"


def _canvas_catalog() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "object": "extension",
                "id": "omnigent.canvas",
                "display_name": "Canvas",
                "distribution": "omnigent-canvas",
                "version": "0.1.0",
                "extension_api": 1,
                "status": "enabled",
                "permissions": ["navigation", "sessions.read", "storage.user"],
                "pages": [
                    {
                        "id": "omnigent.canvas.home",
                        "title": "Canvas",
                        "route": "canvas",
                        "view": "canvas",
                    }
                ],
                "primary_navigation": [
                    {
                        "id": "omnigent.canvas.primary-nav",
                        "label": "Canvas",
                        "page": "omnigent.canvas.home",
                        "icon": "panels-top-left",
                        "order": 350,
                        "when": None,
                    }
                ],
                "browser": {
                    "declared": True,
                    "has_styles": True,
                    "digest": "e2e-canvas",
                    "script_url": "/v1/extensions/omnigent.canvas/assets/e2e-canvas/extension.js",
                    "style_url": "/v1/extensions/omnigent.canvas/assets/e2e-canvas/extension.css",
                },
            }
        ],
    }


def _publish_status(base_url: str, session_id: str, status: str) -> None:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": status}},
        timeout=10.0,
    )
    response.raise_for_status()


def test_canvas_card_updates_status_without_refresh(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A real session update reaches the sandboxed Canvas without a button click."""
    base_url, session_id = seeded_session
    marker = f"canvas-live-{uuid.uuid4().hex[:8]}"
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": marker},
        timeout=10.0,
    )
    response.raise_for_status()

    canvas_list_reads: list[str] = []

    def record_request(request: Request) -> None:
        parsed = urlparse(request.url)
        query = parse_qs(parsed.query)
        if (
            parsed.path == "/v1/sessions"
            and query.get("limit") == ["25"]
            and query.get("kind") == ["default"]
            and query.get("include_archived") == ["false"]
        ):
            canvas_list_reads.append(request.url)

    def serve_catalog(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_canvas_catalog()),
        )

    def serve_asset(route: Route) -> None:
        name = route.request.url.rsplit("/", 1)[-1]
        content_type = "text/javascript" if name.endswith(".js") else "text/css"
        route.fulfill(path=_CANVAS_DIST / name, content_type=content_type)

    page.on("request", record_request)
    page.route("**/v1/extensions", serve_catalog)
    page.route("**/v1/extensions/omnigent.canvas/assets/**", serve_asset)
    page.goto(f"{base_url}/extensions/omnigent.canvas/canvas")

    canvas = page.frame_locator('iframe[title="Canvas"]')
    card = canvas.locator(".session-card").filter(has_text=marker)
    expect(card).to_have_attribute("data-status", "idle", timeout=20_000)
    expect(card.locator(".session-status-text")).to_have_text("Idle")
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    deadline = time.monotonic() + 10
    while len(canvas_list_reads) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert len(canvas_list_reads) >= 2, "Canvas subscription did not complete its baseline refresh"

    try:
        _publish_status(base_url, session_id, "running")
        expect(card).to_have_attribute("data-status", "running", timeout=20_000)
        expect(card.locator(".session-status-text")).to_have_text("Running")
    finally:
        _publish_status(base_url, session_id, "idle")
