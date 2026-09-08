"""Canvas startup renders a preview and reconciles canonical session pages."""

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


@pytest.mark.parametrize("cache_has_more", [True, False])
def test_canvas_reconciles_a_sparse_cached_preview(
    page: Page,
    live_server: str,
    tmp_path: Path,
    cache_has_more: bool,
) -> None:
    """A cached A,D preview must not skip B,C or duplicate A,D on reconciliation."""
    titles = [
        "Plan the release",
        "Review pagination",
        "Document the rollout",
        "Verify SDK contracts",
        "Check saved layouts",
        "Run browser tests",
    ]
    sessions = [
        _session(f"canvas-{index}", title, 10 - index) for index, title in enumerate(titles)
    ]
    cached = [sessions[0], sessions[3]]
    catalog = json.loads(json.dumps(_CATALOG))
    catalog["data"][0]["primary_navigation"] = [
        {
            "id": "omnigent.canvas.nav",
            "label": "Canvas",
            "page": "omnigent.canvas.home",
            "icon": "dashboard",
            "order": 0,
            "when": None,
        }
    ]
    canonical_queries: list[dict[str, list[str]]] = []
    canvas = page.frame_locator('iframe[title="Canvas"]')

    def serve_sessions(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if query.get("kind") == ["default"]:
            canonical_queries.append(query)
            assert "after" not in query
            assert query["limit"] == ["1000"]
            if len(canonical_queries) == 1:
                expect(canvas.get_by_text("2 sessions", exact=True)).to_be_visible()
                expect(canvas.get_by_role("status", name="Loading sessions")).to_be_visible()
                page.screenshot(path=str(tmp_path / "canvas-cached-preview.png"))
                canvas.locator(".canvas-toolbar").screenshot(
                    path=str(tmp_path / "canvas-loading-count.png")
                )
            else:
                expect(canvas.get_by_text("6 sessions", exact=True)).to_be_visible()
                expect(canvas.get_by_role("status", name="Loading sessions")).to_have_count(0)
            data, has_more = sessions, False
        else:
            data, has_more = cached, cache_has_more
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"object": "list", "data": data, "has_more": has_more, "last_id": data[-1]["id"]}
            ),
        )

    page.route("**/v1/extensions", lambda route: route.fulfill(json=catalog))
    page.route(
        "**/e2e-canvas/extension.js",
        lambda route: route.fulfill(content_type="text/javascript", body=_CANVAS_SCRIPT),
    )
    page.route(
        "**/e2e-canvas/extension.css",
        lambda route: route.fulfill(content_type="text/css", body=_CANVAS_STYLES),
    )
    page.route("**/v1/sessions?*", serve_sessions)
    page.goto(live_server)
    expect(page.get_by_text(titles[0], exact=True).first).to_be_visible()
    page.get_by_role("link", name="Canvas", exact=True).click()

    expect(canvas.get_by_text("6 sessions", exact=True)).to_be_visible(timeout=10_000)
    for title in titles:
        expect(canvas.get_by_text(title, exact=True)).to_have_count(1)
    expect(canvas.get_by_role("alert")).to_have_count(0)
    expect(canvas.get_by_role("status", name="Loading sessions")).to_have_count(0)
    assert len(canonical_queries) == 1
    page.screenshot(path=str(tmp_path / "canvas-complete.png"))

    with page.expect_response(
        lambda response: (
            "/v1/sessions?" in response.url
            and parse_qs(urlparse(response.url).query).get("kind") == ["default"]
        )
    ):
        canvas.locator(".canvas-shell").evaluate("() => window.dispatchEvent(new Event('focus'))")
    expect(canvas.get_by_role("status", name="Loading sessions")).to_have_count(0)
    expect(canvas.get_by_text("6 sessions", exact=True)).to_be_visible()
    assert len(canonical_queries) == 2


def _wait_for_centered_canvas(page: Page, card_count: int) -> None:
    iframe = page.locator('iframe[title="Canvas"]').element_handle()
    assert iframe is not None
    frame = iframe.content_frame()
    assert frame is not None
    frame.wait_for_function(
        """(count) => {
          const flow = document.querySelector('.canvas-flow');
          const cards = [...document.querySelectorAll('.session-card')];
          if (!flow || cards.length !== count) return false;
          const bounds = flow.getBoundingClientRect();
          const rects = cards.map(card => card.getBoundingClientRect());
          const centerX =
            (Math.min(...rects.map(r => r.left)) + Math.max(...rects.map(r => r.right))) / 2;
          const centerY =
            (Math.min(...rects.map(r => r.top)) + Math.max(...rects.map(r => r.bottom))) / 2;
          return Math.abs(centerX - (bounds.left + bounds.width / 2)) < 2 &&
            Math.abs(centerY - (bounds.top + bounds.height / 2)) < 2;
        }""",
        arg=card_count,
        timeout=5_000,
    )


@pytest.mark.parametrize("restore_after_resize", [False, True])
def test_canvas_centers_cards_when_opening_a_project_tab(
    page: Page,
    live_server: str,
    tmp_path: Path,
    restore_after_resize: bool,
) -> None:
    """A project opens centered on its cards, not on the previous canvas's bounds."""
    catalog = json.loads(json.dumps(_CATALOG))
    catalog["data"][0]["permissions"].append("projects.read")
    projects = [{"id": "project-release", "name": "Release", "icon": None}]
    sessions = [
        _session(f"main-{index}", f"Main session {index}", 20 - index) for index in range(9)
    ]
    project_session = {
        **_session("project-session", "Review the release", 1),
        "project_id": "project-release",
    }
    sessions.append(project_session)
    page.route("**/v1/extensions", lambda route: route.fulfill(json=catalog))
    page.route(
        "**/e2e-canvas/extension.js",
        lambda route: route.fulfill(content_type="text/javascript", body=_CANVAS_SCRIPT),
    )
    page.route(
        "**/e2e-canvas/extension.css",
        lambda route: route.fulfill(content_type="text/css", body=_CANVAS_STYLES),
    )
    page.route(
        "**/v1/sessions?*",
        lambda route: route.fulfill(
            json={"object": "list", "data": sessions, "has_more": False, "last_id": None}
        ),
    )
    page.route(
        "**/v1/projects",
        lambda route: route.fulfill(json={"object": "list", "data": projects}),
    )
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=projects))
    if restore_after_resize:
        page.set_viewport_size({"width": 1960, "height": 1200})
    page.goto(f"{live_server}/extensions/omnigent.canvas/canvas")
    canvas = page.frame_locator('iframe[title="Canvas"]')
    expect(canvas.get_by_text("9 sessions", exact=True)).to_be_visible()
    canvas.get_by_role("tab", name="Release", exact=True).click()
    expect(canvas.get_by_text("1 session", exact=True)).to_be_visible()
    card = canvas.locator(".session-card")
    expect(card).to_have_count(1)
    expect(card).to_be_visible()
    _wait_for_centered_canvas(page, 1)
    if restore_after_resize:
        # Pan away and back to save the centered view as a deliberate user view.
        flow_bounds = canvas.locator(".canvas-flow").bounding_box()
        assert flow_bounds is not None
        x, y = flow_bounds["x"] + 60, flow_bounds["y"] + 60
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 40, y + 40, steps=4)
        page.mouse.move(x, y, steps=4)
        page.mouse.up()
        page.evaluate(
            """async () => {
              const db = await new Promise((resolve) => {
                const request = indexedDB.open('omnigent-extensions', 1);
                request.onsuccess = () => resolve(request.result);
              });
              try {
                for (let attempt = 0; attempt < 100; attempt++) {
                  const records = await new Promise((resolve) => {
                    const request = db.transaction('values').objectStore('values').getAll();
                    request.onsuccess = () => resolve(request.result);
                  });
                  if (records.some(({ key }) => key.endsWith('.project-release'))) return;
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                throw new Error('Project viewport was not saved');
              } finally {
                db.close();
              }
            }"""
        )
        canvas.get_by_role("tab", name="Main", exact=True).click()
        expect(canvas.get_by_text("9 sessions", exact=True)).to_be_visible()
        page.set_viewport_size({"width": 1480, "height": 900})
        _wait_for_centered_canvas(page, 9)
        canvas.get_by_role("tab", name="Release", exact=True).click()
        expect(canvas.get_by_text("1 session", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / "canvas-project-tab.png"))
    expect(canvas.locator(".canvas-flow")).to_contain_text("Review the release")
    _wait_for_centered_canvas(page, 1)
