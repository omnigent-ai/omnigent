"""The Design page lists slide decks as cards and opens each one in the studio."""

from __future__ import annotations

import base64
import json
import re
import shutil
import urllib.parse
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


def test_design_page_opens_a_seeded_deck_in_the_studio(
    page: Page,
    seeded_deck: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    """The nav opens Design, the seeded deck is a card, and the card opens the studio."""
    base_url, session_id, deck_path = seeded_deck
    deck_name = deck_path.removesuffix(".slides.html")
    _stub_server_info(page, design=True)
    page.set_viewport_size({"width": 1400, "height": 900})

    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("design-nav").click()

    expect(page).to_have_url(re.compile(r"/design$"))
    expect(page.get_by_role("heading", name="Design", exact=True)).to_be_visible()
    expect(page.get_by_text("Slides your agents made, on brand")).to_be_visible()
    card = page.get_by_role("link", name=re.compile(deck_name))
    expect(card).to_be_visible(timeout=30_000)
    card.click()

    expect(page).to_have_url(re.compile(rf"/design\?session=.+&file={re.escape(deck_path)}$"))
    preview = page.get_by_role("region", name="Deck preview")
    expect(preview.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    deck = preview.frame_locator('iframe[title="Slide deck"]')
    expect(deck.locator("#s1")).to_be_visible()
    expect(page.get_by_role("complementary", name="Design chat")).to_be_visible()
    open_in_session = page.get_by_role("link", name="Open in session")
    expect(open_in_session).to_have_attribute("href", re.compile(rf"\?file={deck_path}$"))
    page.screenshot(path=str(tmp_path / "design-studio-desktop.png"))

    # Full hides the chat, and the view is in the URL, so a reload keeps it.
    page.get_by_role("button", name="Full", exact=True).click()
    expect(page).to_have_url(re.compile(r"&view=full$"))
    expect(page.get_by_role("complementary", name="Design chat")).to_have_count(0)
    page.reload()
    expect(preview.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    expect(page.get_by_role("complementary", name="Design chat")).to_have_count(0)


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


def _stub_landing(page: Page, sessions: list[dict[str, object]]) -> None:
    _stub_server_info(page, design=True)
    page.route(
        "**/v1/sessions?*",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "data": sessions,
                "first_id": sessions[0]["id"],
                "last_id": None,
                "has_more": False,
            }
        ),
    )
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=[]))
    page.route("**/resources/environments/default/search?*", _search)
    page.route("**/resources/environments/default/filesystem/**", _file)


def test_design_page_on_a_phone_shows_unavailable_and_returns_to_the_list(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Phone: cards fill the screen, a deck opens full screen, Chat toggles, back returns."""
    _stub_landing(
        page,
        [_session("online", "/work/site", 2), _session("offline", "/work/offline-app", 1)],
    )
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
    page.screenshot(path=str(tmp_path / "design-phone-landing.png"))

    site.get_by_role("link", name=re.compile("pitch")).click()
    expect(page.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    expect(page.get_by_role("region", name="site")).to_have_count(0)
    expect(page.get_by_role("complementary", name="Design chat")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "design-phone-preview.png"))

    page.get_by_role("button", name="Chat").click()
    expect(page).to_have_url(re.compile(r"&view=chat$"))
    expect(page.get_by_role("complementary", name="Design chat")).to_be_visible()
    expect(page.get_by_role("region", name="Deck preview")).to_have_count(0)
    page.get_by_role("button", name="Close chat").click()
    expect(page.get_by_text("1 / 2")).to_be_visible(timeout=15_000)

    # SPA history steps fire no load event, so page.go_back() would wait forever.
    page.evaluate("history.back()")
    expect(page).to_have_url(re.compile(r"/design$"))
    expect(page.get_by_role("region", name="site")).to_be_visible()

    page.evaluate("history.forward()")
    expect(page.get_by_text("1 / 2")).to_be_visible(timeout=15_000)
    page.get_by_role("link", name="Back to designs").click()
    expect(page).to_have_url(re.compile(r"/design$"))
    expect(page.get_by_role("region", name="offline-app")).to_be_visible()


_HOST = {"host_id": "host_e2e", "name": "e2e-host", "owner": "local", "status": "online"}


def _host_listing(route: Route) -> None:
    path = route.request.url.split("/filesystem", 1)[1].split("?")[0]
    if path.endswith("/.omnigent/design-kit"):
        route.fulfill(
            json={
                "object": "list",
                "data": [
                    {
                        "name": "kit.json",
                        "path": f"{path}/kit.json",
                        "type": "file",
                        "bytes": 2,
                        "modified_at": 1,
                    }
                ],
                "has_more": False,
            }
        )
        return
    route.fulfill(status=404, json={"detail": "not found"})


def test_new_design_creates_a_session_and_opens_the_studio(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """New design posts the session create then the first message and opens the studio."""
    _stub_landing(page, [_session("online", "/work/site", 2)])
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(json={"hosts": [_HOST]}),
    )
    page.route("**/v1/hosts/host_e2e/filesystem/**", _host_listing)
    posted: dict[str, object] = {}

    def _create(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        posted["create"] = route.request.post_data_json
        route.fulfill(
            json={"id": "conv_design", "agent_id": "ag", "status": "idle", "created_at": 1}
        )

    def _event(route: Route) -> None:
        posted["event"] = route.request.post_data_json
        route.fulfill(status=202, json={"queued": True})

    page.route("**/v1/sessions", _create)
    page.route("**/v1/sessions/conv_design/events", _event)
    # The last folder used for a design on this host prefills the dialog.
    page.add_init_script(
        "localStorage.setItem('omnigent.design.defaults', JSON.stringify("
        "{hostId: 'host_e2e', folders: {host_e2e: '/work/site'}}))"
    )
    page.set_viewport_size({"width": 1400, "height": 900})

    page.goto(f"{live_server}/design")
    expect(page.get_by_role("link", name=re.compile("pitch"))).to_be_visible(timeout=30_000)
    page.get_by_role("button", name="Weekly status update").click()

    dialog = page.get_by_role("dialog", name="New design")
    expect(dialog.get_by_label("Prompt")).to_have_value("Weekly status update")
    expect(dialog.get_by_text("/work/site")).to_be_visible()
    expect(dialog.get_by_text("Kit found")).to_be_visible()
    page.screenshot(path=str(tmp_path / "design-new-dialog.png"))
    dialog.get_by_role("button", name="Create").click()

    expect(page).to_have_url(
        re.compile(
            r"/design\?session=conv_design&file=decks%2Fweekly-status-update\.slides\.html$"
        )
    )
    expect(page.get_by_text("Waiting for the first slide")).to_be_visible(timeout=15_000)
    expect(page.get_by_role("complementary", name="Design chat")).to_be_visible()
    page.screenshot(path=str(tmp_path / "design-studio-waiting.png"))

    create = posted["create"]
    assert isinstance(create, dict)
    assert create["host_id"] == "host_e2e"
    assert create["workspace"] == "/work/site"
    assert create["agent_id"]
    event = posted["event"]
    assert isinstance(event, dict)
    assert event["type"] == "message"
    text = event["data"]["content"][0]["text"]
    assert text.startswith("Weekly status update\n\nUse the slide-decks skill.")
    assert "`decks/weekly-status-update.slides.html`" in text


_DS_FIXTURE = _REPO_ROOT / "web" / "src" / "test" / "fixtures" / "design-system"
_DS_FOLDER = "/brand/fixture"
_DS_DECK = """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <style>h1 { color: var(--fx-primary, #000); font-family: var(--fx-font, serif); }</style>
  </head>
  <body>
    <section id="s1"><h1>Branded</h1><img id="logo" src="ds:assets/logo.svg" alt="" /></section>
  </body>
</html>
"""


def _file_content(path: str, data: bytes, content_type: str) -> dict[str, object]:
    text = data.decode("utf-8", errors="strict") if b"\0" not in data else None
    return {
        "object": "session.environment.filesystem.file_content",
        "path": path,
        "content_type": content_type,
        "encoding": "utf-8" if text is not None else "base64",
        "content": text if text is not None else base64.b64encode(data).decode(),
        "bytes": len(data),
    }


def _design_system_files(route: Route) -> None:
    """Serve a deck, a full design-system pointer, and the fixture under ``_DS_FOLDER``."""
    url = urllib.parse.urlsplit(route.request.url)
    path = urllib.parse.unquote(url.path.split("/filesystem/", 1)[1])
    if "base=host" in url.query and f"/{path}".startswith(f"{_DS_FOLDER}/"):
        file = _DS_FIXTURE / f"/{path}".removeprefix(f"{_DS_FOLDER}/")
        if file.is_file():
            route.fulfill(
                json=_file_content(f"/{path}", file.read_bytes(), "application/octet-stream")
            )
            return
    elif path == "decks/pitch.slides.html":
        route.fulfill(json=_file_content(path, _DS_DECK.encode(), "text/html"))
        return
    elif path == ".omnigent/design-system.json":
        pointer = {"path": _DS_FOLDER, "kind": "full", "name": "Fixture Brand"}
        route.fulfill(json=_file_content(path, json.dumps(pointer).encode(), "application/json"))
        return
    route.fulfill(status=404, json={"error": {"code": "not_found"}})


def test_design_system_brands_the_landing_and_the_deck(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """A full design-system pointer names the group and injects tokens, fonts, and ds: assets."""
    _stub_landing(page, [_session("online", "/work/site", 2)])
    page.route("**/resources/environments/default/filesystem/**", _design_system_files)
    page.route(
        "**/v1/sessions/online?*",
        lambda route: route.fulfill(json=_session("online", "/work/site", 2)),
    )
    page.set_viewport_size({"width": 1400, "height": 900})

    page.goto(f"{live_server}/design")
    site = page.get_by_role("region", name="site")
    badge = site.get_by_title("Design system: Fixture Brand (full)")
    expect(badge).to_be_visible(timeout=30_000)
    expect(badge).to_contain_text("Full")
    site.get_by_role("link", name=re.compile("pitch")).click()

    preview = page.get_by_role("region", name="Deck preview")
    expect(preview.get_by_title("Design system: Fixture Brand")).to_be_visible(timeout=15_000)
    deck = preview.frame_locator('iframe[title="Slide deck"]')
    heading = deck.get_by_role("heading", name="Branded")
    expect(heading).to_have_css("color", "rgb(11, 95, 255)")
    expect(heading).to_have_css("font-family", re.compile("Fixture Sans"))
    logo = deck.locator("#logo")
    expect(logo).to_have_attribute("src", re.compile(r"^data:image/svg\+xml;base64,"))
    assert logo.evaluate("img => img.complete && img.naturalWidth") == 10
    expect(preview.get_by_role("status")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "design-system-studio.png"))


def test_new_design_with_a_design_system_writes_the_pointer(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """A recent design system is offered, its pointer is written, and the agent follows it."""
    _stub_landing(page, [_session("online", "/work/site", 2)])
    page.route("**/v1/hosts", lambda route: route.fulfill(json={"hosts": [_HOST]}))
    page.route("**/v1/hosts/host_e2e/filesystem/**", _host_listing)
    posted: dict[str, object] = {}

    def _create(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        route.fulfill(
            json={"id": "conv_design", "agent_id": "ag", "status": "idle", "created_at": 1}
        )

    def _write(route: Route) -> None:
        if route.request.method != "PUT":
            route.fallback()
            return
        posted["write"] = (route.request.url, route.request.post_data_json)
        route.fulfill(json={"object": "session.environment.filesystem.write_result"})

    def _event(route: Route) -> None:
        posted["event"] = route.request.post_data_json
        route.fulfill(status=202, json={"queued": True})

    page.route("**/v1/sessions", _create)
    page.route("**/v1/sessions/conv_design/resources/environments/default/filesystem/**", _write)
    page.route("**/v1/sessions/conv_design/events", _event)
    page.add_init_script(
        "localStorage.setItem('omnigent.design.defaults', JSON.stringify("
        "{hostId: 'host_e2e', folders: {host_e2e: '/work/plain'}}));"
        "localStorage.setItem('omnigent.design.systems', JSON.stringify("
        "{host_e2e: [{path: '/brand/acme', kind: 'full', name: 'Acme'}]}))"
    )
    page.set_viewport_size({"width": 1400, "height": 900})

    page.goto(f"{live_server}/design")
    expect(page.get_by_role("link", name=re.compile("pitch"))).to_be_visible(timeout=30_000)
    page.get_by_role("button", name="New design").click()

    dialog = page.get_by_role("dialog", name="New design")
    dialog.get_by_label("Prompt").fill("Quarterly review")
    # The stubbed host reports a kit in the folder, so the kit is the default.
    expect(dialog.get_by_label("Design system")).to_contain_text("Folder kit")
    dialog.get_by_label("Design system").click()
    expect(page.get_by_role("option", name="Choose folder")).to_be_visible()
    expect(page.get_by_role("option", name="None")).to_be_visible()
    page.get_by_role("option", name="Acme (full)").click()
    expect(dialog.get_by_label("Design system")).to_contain_text("Acme (full)")
    page.screenshot(path=str(tmp_path / "design-new-dialog-system.png"))
    dialog.get_by_role("button", name="Create").click()

    expect(page).to_have_url(re.compile(r"/design\?session=conv_design&file="), timeout=15_000)
    url, body = posted["write"]  # type: ignore[misc]
    assert str(url).split("?")[0].endswith("/filesystem/.omnigent/design-system.json")
    assert isinstance(body, dict)
    assert json.loads(body["content"]) == {"path": "/brand/acme", "kind": "full", "name": "Acme"}
    event = posted["event"]
    assert isinstance(event, dict)
    text = event["data"]["content"][0]["text"]
    assert text.endswith(
        "Follow the design system at `/brand/acme` (`full`). Read its SKILL.md first."
    )
