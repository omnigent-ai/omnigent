"""Browser e2e for the Project settings dialog (project ``config`` defaults).

A project carries owner-private session defaults in its ``config`` JSON
(``{ host_id?, workspace?, agent_id?, use_worktree?, base_branch? }``). The "Project settings"
dialog (``web/src/shell/ProjectSettingsDialog.tsx``, reached from the
project-folder kebab) reads and writes that config via ``GET /v1/projects/{id}``
and ``PATCH /v1/projects/{id}``.

This drives the real persistence round-trip the ``ProjectSettingsDialog`` unit
tests mock out: open the dialog → flip the "Random worktree" toggle ON → Save
(``PATCH``) → reopen and confirm the fetched config seeds the toggle back ON.
Worktrees are opt-in, so a toggle-ON stores ``use_worktree: true``.

The project is created directly via ``POST /v1/projects`` (an empty first-class
project needs no member session, and this sidesteps the flaky row-kebab
move-to-project dance). The working-directory / host prefill can't be exercised
here — the e2e_ui runner registers no host to browse — so this test stays on the
host-free path (the worktree toggle is a plain Switch, no host needed).
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Page, Route, expect

from tests._helpers.picker_routes import OWN_AGENTS


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
    """Open the folder kebab → "Project settings" for *project*."""
    actions = page.get_by_role("button", name=f"Project actions for {project}", exact=True)
    expect(actions).to_be_visible()
    actions.click()
    page.get_by_test_id("project-settings").click()
    # The dialog's Save button confirms the editor mounted + the config fetch
    # settled (Save is disabled while loading).
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def test_project_static_sandbox_default_wakes_and_reuses(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Real project persistence; controller wake and session launch are mocked."""
    base_url, session_id = seeded_session
    project = f"Static sandbox project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    saved = {
        "host_id": "saved-static",
        "name": "controller-machine-id",
        "display_name": "Project sandbox",
        "owner": "e2e",
        "status": "offline",
        "sandbox_attached": True,
        "sandbox_provider": "static",
    }
    wake_calls = []
    creates = []
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {"host_id": "laptop", "name": "Laptop", "owner": "e2e", "status": "online"},
                    {
                        "host_id": "adhoc",
                        "name": "Ad hoc sandbox",
                        "owner": "e2e",
                        "status": "online",
                        "sandbox_provider": "static",
                    },
                    saved,
                ]
            }
        ),
    )

    def wake(route: Route) -> None:
        wake_calls.append(route.request.url)
        saved["status"] = "online"
        route.fulfill(json={"host_id": saved["host_id"]})

    def create(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        creates.append(route.request.post_data_json)
        route.fulfill(json={"id": session_id})

    page.route("**/v1/hosts/saved-static/wake", wake)
    page.route(
        "**/v1/hosts/*/harnesses/*/model-options", lambda route: route.fulfill(json={"models": []})
    )
    page.route("**/v1/hosts/*/worktrees?*", lambda route: route.fulfill(json={"data": []}))
    page.route(
        re.compile(r"/v1/hosts/[^/]+/filesystem"),
        lambda route: route.fulfill(json={"path": "/work/project", "entries": []}),
    )
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": "ag_demo",
                        "name": "polly",
                        "display_name": "Polly",
                        "harness": "claude-sdk",
                        "skills": [],
                    }
                ]
            }
        ),
    )
    page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))
    page.route(re.compile(r"/v1/sessions(\?.*)?$"), create)
    page.route(
        "**/v1/sessions/*/events",
        lambda route: route.fulfill(json={"queued": True, "item_id": "demo"}),
    )
    page.goto(f"{base_url}/c/{session_id}")
    _open_project_settings(page, project)
    page.get_by_test_id("project-settings-host").click()
    expect(page.get_by_role("option", name="Ad hoc sandbox", exact=True)).to_have_count(0)
    page.get_by_role("option", name="Project sandbox (offline)", exact=True).click()
    page.get_by_test_id("project-settings-workspace").fill("/work/project")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)
    config = _get_project_config(base_url, project_id)
    assert config["host_id"] == "saved-static"
    assert config["workspace"] == "/work/project"
    assert wake_calls == []

    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-host")).to_contain_text(
        "Project sandbox (offline)"
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    for prompt in ("Investigate using my existing checkout", "Continue on the same sandbox"):
        page.get_by_role("link", name=f"New session in {project}", exact=True).click()
        expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
            "aria-label", re.compile("Project sandbox")
        )
        page.get_by_test_id("new-chat-landing-host-chip").click()
        expect(page.get_by_test_id("new-chat-landing-host-saved-static")).to_have_attribute(
            "data-active", "true"
        )
        expect(page.get_by_test_id("new-chat-landing-host-adhoc")).to_have_count(0)
        page.keyboard.press("Escape")
        page.get_by_test_id("new-chat-landing-input").fill(prompt)
        expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
        page.get_by_test_id("new-chat-landing-submit").click()
        expect(page).to_have_url(f"{base_url}/c/{session_id}")
    assert len(wake_calls) == 1
    assert len(creates) == 2
    assert all(
        body["host_id"] == "saved-static" and body["project_id"] == project_id for body in creates
    )
    assert all("host_type" not in body for body in creates)


def test_project_settings_worktree_toggle_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Turning "Random worktree" ON persists and re-seeds on reopen.

    Opens an empty project's settings, flips the opt-in worktree toggle ON, and
    Saves (``PATCH /v1/projects/{id}`` with ``use_worktree:true``). Reopening the
    dialog fetches the stored config and seeds the toggle back ON — proving the
    write reached the server and the read seeds from it.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    # Open settings; the worktree toggle defaults OFF (opt-in).
    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "unchecked")

    # Flip it ON and save.
    toggle.click()
    expect(toggle).to_have_attribute("data-state", "checked")
    page.get_by_test_id("project-settings-save").click()

    # The dialog closes on a successful save.
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — the stored config seeds the toggle back ON.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "checked"
    )


def test_project_settings_clears_worktree_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Turning the toggle back OFF clears the stored default.

    Starts from a project whose ``config`` has ``use_worktree: true``, opens
    settings (toggle seeds ON), flips it OFF, and Saves. An all-default dialog
    clears to ``{}``, so reopening seeds the toggle OFF.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    _set_project_config(base_url, project_id, {"use_worktree": True})

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "checked")

    # Flip OFF and save → config clears to {}.
    toggle.click()
    expect(toggle).to_have_attribute("data-state", "unchecked")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — no stored default, toggle seeds OFF.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "unchecked"
    )


def test_project_settings_preserves_icon_on_save(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Saving settings preserves config keys owned by other project dialogs."""
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    _set_project_config(base_url, project_id, {"icon": "🔥", "use_worktree": True})

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "checked"
    )
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    assert _get_project_config(base_url, project_id) == {"icon": "🔥", "use_worktree": True}


def test_project_settings_base_branch_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The base-branch field shows only with the worktree default on, and persists.

    The base branch only forks a worktree, so its field is hidden until the
    "Random worktree" toggle is ON. Flipping the toggle reveals it; typing a
    branch + Save stores ``base_branch`` alongside ``use_worktree:true``.
    Reopening fetches the stored config and seeds both back.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    # Worktree toggle defaults OFF, so the base-branch field is hidden.
    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "unchecked")
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_count(0)

    # Turning the worktree default ON reveals the base-branch field; fill + save.
    toggle.click()
    base_input = page.get_by_test_id("project-settings-base-branch")
    expect(base_input).to_be_visible()
    base_input.fill("develop")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — the stored config seeds the toggle ON and the branch back.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "checked"
    )
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_value("develop")


def test_project_settings_drops_base_branch_when_worktree_off(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Turning the worktree default OFF drops a previously stored base branch.

    Starts from a project whose ``config`` has ``use_worktree:true`` and a
    ``base_branch``. The base-branch field is only meaningful with worktrees on,
    so flipping the toggle OFF clears the whole config to ``{}`` — reopening
    seeds the toggle OFF and (once toggled back on) shows a blank base branch.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    _set_project_config(base_url, project_id, {"use_worktree": True, "base_branch": "develop"})

    page.goto(f"{base_url}/c/{session_id}")

    # Seeded ON → the base-branch field shows its stored value.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_value("develop")

    # Flip the worktree default OFF and save → config clears to {}.
    page.get_by_test_id("project-settings-worktree").click()
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — toggle seeds OFF; toggling back on shows a blank base branch.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "unchecked"
    )
    page.get_by_test_id("project-settings-worktree").click()
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_value("")
