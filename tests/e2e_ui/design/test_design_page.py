"""The Design page lists slide decks from recent sessions and renders the selected one."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

# Seeded files land under the server's cwd, the repo root (see test_slides_viewer.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_DECK_CONTENT = """\
<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8" /><title>Design fixture</title></head>
  <body>
    <section id="s1"><h1>Design slide one</h1></section>
    <section id="s2"><h1>Design slide two</h1></section>
  </body>
</html>
"""


def _stub_server_info(page: Page, *, design: bool) -> None:
    """Advertise one deterministic ``design`` release-feature value."""
    body = json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "features": {"design": design, "canvas": False, "usage_page": False},
            "harness_install_enabled": False,
            "installable_harnesses": [],
        }
    )
    page.route(
        "**/v1/info",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


@pytest.fixture
def seeded_deck(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, str]]:
    """Write a uniquely named deck into the session's workspace; yield ``(base, id, path)``."""
    base_url, session_id = seeded_session
    deck_path = f"design-e2e-{uuid.uuid4().hex[:8]}.slides.html"
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{deck_path}",
        json={"content": _DECK_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, deck_path)
    finally:
        (_REPO_ROOT / deck_path).unlink(missing_ok=True)
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_design_page_is_absent_while_the_feature_is_off(page: Page, live_server: str) -> None:
    """With the flag off there is no nav entry and a deep link shows Page not found."""
    _stub_server_info(page, design=False)

    page.goto(f"{live_server}/design")

    expect(page.get_by_role("heading", name="Page not found")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("design-nav")).to_have_count(0)


def test_design_page_lists_and_renders_a_seeded_deck(
    page: Page,
    seeded_deck: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    """The nav opens Design, the seeded deck is listed, and selecting it renders it."""
    base_url, session_id, deck_path = seeded_deck
    deck_name = deck_path.removesuffix(".slides.html")
    _stub_server_info(page, design=True)
    page.set_viewport_size({"width": 1400, "height": 900})

    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("design-nav").click()

    expect(page).to_have_url(re.compile(r"/design$"))
    expect(page.get_by_role("heading", name="Design", exact=True)).to_be_visible()
    expect(page.get_by_text("Select a deck to view it here.")).to_be_visible()
    row = page.get_by_role("link", name=re.compile(deck_name))
    expect(row).to_be_visible(timeout=30_000)
    row.click()

    expect(page).to_have_url(re.compile(rf"/design\?session=.+&file={re.escape(deck_path)}$"))
    expect(row).to_have_attribute("aria-current", "true")
    viewer = page.get_by_role("region", name="Deck viewer")
    expect(viewer.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    deck = viewer.frame_locator('iframe[title="Slide deck"]')
    expect(deck.locator("#s1")).to_be_visible()
    open_in_session = viewer.get_by_role("link", name="Open in session")
    expect(open_in_session).to_have_attribute("href", re.compile(rf"\?file={deck_path}$"))
    page.screenshot(path=str(tmp_path / "design-desktop.png"))

    # The selection is in the URL, so a reload reopens the same deck.
    page.reload()
    expect(viewer.get_by_text("1 / 2")).to_be_visible(timeout=15_000)


def _session(session_id: str, workspace: str, updated_at: int) -> dict[str, object]:
    return {
        "id": session_id,
        "object": "conversation",
        "title": f"Session {session_id}",
        "status": "idle",
        "created_at": 1,
        "updated_at": updated_at,
        "labels": {},
        "permission_level": None,
        "workspace": workspace,
        "git_branch": None,
        "project_id": None,
        "archived": False,
        "parent_session_id": None,
    }


def _search(route: Route) -> None:
    if "/sessions/offline/" in route.request.url:
        route.fulfill(status=503, json={"error": {"code": "runner_unavailable"}})
        return
    route.fulfill(
        json={
            "object": "list",
            "data": [
                {
                    "id": "pitch",
                    "name": "pitch.slides.html",
                    "path": "decks/pitch.slides.html",
                    "type": "file",
                    "bytes": len(_DECK_CONTENT),
                    "modified_at": 1,
                }
            ],
            "has_more": False,
        }
    )


def _file(route: Route) -> None:
    if route.request.url.split("?")[0].endswith("decks/pitch.slides.html"):
        route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": "decks/pitch.slides.html",
                "content_type": "text/html",
                "encoding": "utf-8",
                "content": _DECK_CONTENT,
                "bytes": len(_DECK_CONTENT),
            }
        )
        return
    route.fulfill(status=404, json={"error": {"code": "not_found"}})


def test_design_page_on_a_phone_shows_unavailable_and_returns_to_the_list(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Phone: the list fills the screen, a deck opens full screen, and back returns."""
    sessions = [
        _session("online", "/work/site", 2),
        _session("offline", "/work/offline-app", 1),
    ]
    _stub_server_info(page, design=True)
    page.route(
        "**/v1/sessions?*",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "data": sessions,
                "first_id": "online",
                "last_id": None,
                "has_more": False,
            }
        ),
    )
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=[]))
    page.route("**/resources/environments/default/search?*", _search)
    page.route("**/resources/environments/default/filesystem/**", _file)
    page.set_viewport_size({"width": 390, "height": 844})

    page.goto(f"{live_server}/design")

    offline = page.get_by_role("region", name="offline-app")
    expect(offline.get_by_text("Unavailable: open the session to start its runner")).to_be_visible(
        timeout=30_000
    )
    expect(offline.get_by_role("link", name="Open session")).to_have_attribute(
        "href", re.compile(r"/c/offline$")
    )
    site = page.get_by_role("region", name="site")
    expect(site.get_by_role("link", name="No kit")).to_be_visible()
    page.screenshot(path=str(tmp_path / "design-phone-list.png"))

    site.get_by_role("link", name=re.compile("pitch")).click()
    expect(page.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    expect(page.get_by_role("region", name="site")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "design-phone-deck.png"))

    # SPA history steps fire no load event, so page.go_back() would wait forever.
    page.evaluate("history.back()")
    expect(page).to_have_url(re.compile(r"/design$"))
    expect(page.get_by_role("region", name="site")).to_be_visible()

    page.evaluate("history.forward()")
    expect(page.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="Back to decks").click()
    expect(page).to_have_url(re.compile(r"/design$"))
    expect(page.get_by_role("region", name="offline-app")).to_be_visible()
