"""Canvas startup stays on one spinner until useful content is ready."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

_ROOT = Path(__file__).resolve().parents[3]
_CANVAS_DIST = _ROOT / "extensions" / "canvas" / "src" / "omnigent_canvas" / "dist"
_CANVAS_SCRIPT = _CANVAS_DIST.joinpath("extension.js").read_text()
_CANVAS_STYLES = _CANVAS_DIST.joinpath("extension.css").read_text()

_CATALOG = {
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
            "primary_navigation": [],
            "browser": {
                "declared": True,
                "has_styles": True,
                "digest": "e2e-canvas",
                "script_url": "/e2e-canvas/extension.js",
                "style_url": "/e2e-canvas/extension.css",
            },
        }
    ],
}


def _session(session_id: str, title: str, updated_at: int) -> dict[str, object]:
    return {
        "id": session_id,
        "title": title,
        "status": "idle",
        "created_at": 1,
        "updated_at": updated_at,
        "workspace": "/workspace/canvas",
        "git_branch": None,
        "project_id": None,
        "archived": False,
        "parent_session_id": None,
    }


def _record_startup_states(page: Page) -> None:
    page.add_init_script(
        """
        (() => {
          const state = window.__canvasStartup = {
            notFoundSeen: false,
            loadingCopySeen: false,
            spinnerSeen: false,
            spinnerGapSeen: false,
            readySeen: false,
          };
          const sample = () => {
            const text = document.body?.innerText ?? "";
            state.notFoundSeen ||= text.includes("Page not found");
            state.loadingCopySeen ||=
              /Loading extension|Starting extension|Loading sessions/.test(text);
            const spinner = document.querySelector(
              '[role="status"][aria-label="Loading extension"]',
            );
            const spinnerVisible = Boolean(
              spinner && spinner.getBoundingClientRect().width > 0,
            );
            const host = document.querySelector(".extension-view-host");
            const ready = Boolean(
              host &&
              host.querySelector('iframe[title="Canvas"]') &&
              !host.querySelector(".extension-view-status"),
            );
            if (spinnerVisible) state.spinnerSeen = true;
            if (state.spinnerSeen && !spinnerVisible && !ready) {
              state.spinnerGapSeen = true;
            }
            state.readySeen ||= ready;
          };
          const start = () => {
            sample();
            new MutationObserver(sample).observe(document.documentElement, {
              attributes: true,
              childList: true,
              characterData: true,
              subtree: true,
            });
            setInterval(sample, 20);
          };
          if (document.documentElement) start();
          else window.addEventListener("DOMContentLoaded", start, { once: true });
        })();
        """
    )


def test_canvas_uses_one_spinner_until_initial_content_is_ready(
    page: Page,
    live_server: str,
) -> None:
    """A delayed catalog, bundle, and first page never expose intermediate states."""
    catalog_requests = 0
    canvas_limits: list[int] = []

    def serve_catalog(route: Route) -> None:
        nonlocal catalog_requests
        catalog_requests += 1
        time.sleep(0.4)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_CATALOG))

    def serve_script(route: Route) -> None:
        time.sleep(0.3)
        route.fulfill(status=200, content_type="text/javascript", body=_CANVAS_SCRIPT)

    def serve_styles(route: Route) -> None:
        route.fulfill(status=200, content_type="text/css", body=_CANVAS_STYLES)

    def serve_sessions(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if query.get("kind") != ["default"]:
            # Keep the sidebar from populating the cache Canvas uses for its first paint.
            route.fulfill(status=500, content_type="application/json", body="{}")
            return
        limit = int(query["limit"][0])
        canvas_limits.append(limit)
        if limit == 25:
            time.sleep(0.4)
            body = {
                "object": "list",
                "data": [_session("canvas-first", "First meaningful session", 2)],
                "has_more": True,
                "last_id": "canvas-first",
            }
        else:
            body = {
                "object": "list",
                "data": [_session("canvas-second", "Background session", 1)],
                "has_more": False,
                "last_id": None,
            }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    _record_startup_states(page)
    page.route("**/v1/extensions", serve_catalog)
    page.route("**/e2e-canvas/extension.js", serve_script)
    page.route("**/e2e-canvas/extension.css", serve_styles)
    page.route("**/v1/sessions?*", serve_sessions)

    page.goto(f"{live_server}/extensions/omnigent.canvas/canvas")
    for _ in range(50):
        if catalog_requests:
            break
        if page.get_by_role("heading", name="Page not found").is_visible():
            pytest.skip("The compatibility run is serving a pre-extension SPA")
        page.wait_for_timeout(100)
    assert catalog_requests == 1

    canvas = page.frame_locator('iframe[title="Canvas"]')
    expect(canvas.get_by_role("heading", name="Canvas", exact=True)).to_be_visible(timeout=15_000)
    expect(canvas.get_by_text("First meaningful session", exact=True)).to_be_visible()
    expect(canvas.get_by_text("Background session", exact=True)).to_be_visible()

    parent_state = page.evaluate("window.__canvasStartup")
    frame_state = (
        page.locator('iframe[title="Canvas"]')
        .element_handle()
        .content_frame()
        .evaluate("window.__canvasStartup")
    )
    assert parent_state == {
        "notFoundSeen": False,
        "loadingCopySeen": False,
        "spinnerSeen": True,
        "spinnerGapSeen": False,
        "readySeen": True,
    }
    assert frame_state["loadingCopySeen"] is False
    assert canvas_limits[:2] == [25, 1_000]
