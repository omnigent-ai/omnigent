"""Browser e2e: Project-settings writes vs. the emoji icon picker's own PATCH.

The project icon picker (``ProjectLandingIcon``, hosted on the new-chat project
landing) persists on click: picking an emoji immediately issues its own
``PATCH /v1/projects/{id}`` carrying the whole config blob. The Project
settings dialog builds its Save payload by spreading the cached
``["project-config", id]`` snapshot (fresh for 30s), and the PATCH replaces the
stored config wholesale. So on a slow network a settings Save issued while the
icon PATCH is still in flight sends the pre-icon blob; the server applies the
two writes in send order, the stale blob lands second, and the just-picked
emoji is silently dropped.

``test_settings_save_racing_icon_patch_keeps_icon`` drives that race end to
end: pick an emoji on the project landing, Save the untouched Project settings
dialog while the icon PATCH has not yet reached the server (both writes held
and then delivered in send order, as a throttled uplink does), and require the
icon to survive.

``test_settings_cancel_leaves_stored_config_untouched`` guards the dialog's
discard contract around the same config blob: editing a field and pressing
Cancel must issue no project PATCH and leave the stored config (icon included)
byte-identical.
"""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Page, Request, Route, expect

_FIRE = "\U0001f525"


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _set_project_config(base_url: str, project_id: str, config: dict) -> None:
    """Write a project's stored config via ``PATCH /v1/projects/{id}``."""
    resp = httpx.patch(
        f"{base_url}/v1/projects/{project_id}", json={"config": config}, timeout=10.0
    )
    resp.raise_for_status()


def _get_project_config(base_url: str, project_id: str) -> dict:
    """Read a project's stored config via ``GET /v1/projects/{id}``."""
    resp = httpx.get(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()["config"]


def _open_project_settings(page: Page, project: str) -> None:
    """Open the folder kebab -> "Project settings" for *project*."""
    actions = page.get_by_role("button", name=f"Project actions for {project}", exact=True)
    expect(actions).to_be_visible()
    actions.click()
    page.get_by_test_id("project-settings").click()
    # The dialog's Save button confirms the editor mounted + the config fetch
    # settled (Save is disabled while loading).
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def _pick_landing_emoji(page: Page, glyph: str, search: str) -> None:
    """Pick *glyph* through the landing icon tile's emoji picker."""
    tile = page.get_by_test_id("project-icon-tile")
    expect(tile).to_be_visible(timeout=30_000)
    # Editing is gated until the project list + stored config resolve.
    expect(page.get_by_test_id("project-icon-edit")).to_be_enabled()
    tile.click()
    picker = page.locator("em-emoji-picker")
    expect(picker).to_be_visible()
    picker.locator("input[type=search]").fill(search)
    # emoji-mart labels each result button with the bare glyph.
    picker.locator(f'button[aria-label="{glyph}"]').first.click()


def test_settings_save_racing_icon_patch_keeps_icon(page: Page, live_server: str) -> None:
    """A Save while the picker's PATCH is in flight must not drop the icon.

    Every project PATCH is held un-forwarded while the journey runs, then the
    held writes are delivered in send order — a throttled uplink. The icon
    PATCH therefore commits first and the settings Save lands last; the picked
    emoji must still be stored (and shown on the landing after a reload).
    """
    base_url = live_server
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)

    held: list[Route] = []

    def hold_project_patch(route: Route) -> None:
        if route.request.method == "PATCH":
            held.append(route)
            return
        route.fallback()

    page.route(f"**/v1/projects/{project_id}", hold_project_patch)

    page.goto(f"{base_url}/?project={project}")
    _pick_landing_emoji(page, _FIRE, "fire")

    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert held, "picking an emoji never issued the icon PATCH"
    assert (held[0].request.post_data_json or {}).get("config") == {"icon": _FIRE}

    # Save the (untouched) settings dialog while the icon PATCH is in flight.
    _open_project_settings(page, project)
    save = page.get_by_test_id("project-settings-save")
    save.click()
    deadline = time.monotonic() + 10
    while len(held) < 2 and save.count() > 0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)

    # The throttled writes reach the server in the order they were sent.
    for route in list(held):
        route.fulfill(response=route.fetch())
    expect(save).to_have_count(0)
    page.unroute(f"**/v1/projects/{project_id}")

    config = _get_project_config(base_url, project_id)
    page.goto(f"{base_url}/?project={project}")
    tile = page.get_by_test_id("project-icon-tile")
    expect(tile).to_be_visible(timeout=30_000)
    assert config.get("icon") == _FIRE, (
        f"settings Save dropped the just-picked icon: stored config is {config!r}"
    )
    expect(tile).to_have_text(_FIRE)


def test_settings_cancel_leaves_stored_config_untouched(page: Page, live_server: str) -> None:
    """Editing a settings field and pressing Cancel writes nothing at all."""
    base_url = live_server
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    stored = {"icon": _FIRE, "use_worktree": True}
    _set_project_config(base_url, project_id, stored)

    patches: list[str] = []

    def track_patch(request: Request) -> None:
        if request.method == "PATCH" and f"/v1/projects/{project_id}" in request.url:
            patches.append(request.url)

    page.on("request", track_patch)

    page.goto(f"{base_url}/?project={project}")
    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "checked")
    toggle.click()
    expect(toggle).to_have_attribute("data-state", "unchecked")

    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Cancel").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    assert patches == []
    assert _get_project_config(base_url, project_id) == stored
