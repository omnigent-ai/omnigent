"""Browser e2e for moving a session into a project from the chat header.

The session title's leading folder icon is a Slack-style "Move session"
shortcut (``data-testid="header-project-tag"``): clicking it opens a project
picker whose search box doubles as a create field. Picking a project — or the
"+ Create <name>" row for a typed name with no match — files the session via
``PATCH /v1/sessions/{id}`` with ``{project_id}`` (creating the first-class
``projects`` row on demand). Desktop-only; the kebab keeps the equivalent item
on mobile.

These drive the real chain the ``HeaderProjectTag`` / ``ProjectPicker`` unit
tests mock out: the header tag → the PATCH → the committed ``project_id`` on
``GET /v1/sessions/{id}``, and the sidebar regrouping the row into the project
folder.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


def _await_filed_under(base_url: str, session_id: str, *, timeout: float = 10.0) -> str | None:
    """Poll ``GET /v1/sessions/{id}`` until it reports a ``project_id``.

    The header paints the move optimistically, so the committed membership can
    lag the click by a PATCH round-trip.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        filed_under = snapshot.json().get("project_id")
        if filed_under:
            return filed_under
        time.sleep(0.2)
    return None


def test_header_folder_tag_moves_session_into_existing_project(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The header folder tag files an unfiled session into a picked project."""
    base_url, session_id = seeded_session
    uniq = uuid.uuid4().hex[:6]
    project_name = f"E2E Header Move {uniq}"
    title = f"e2e-header-move-{uniq}"

    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()
    project_id = httpx.post(
        f"{base_url}/v1/projects",
        json={"name": project_name},
        timeout=10.0,
    ).json()["id"]
    # A committed turn settles the header title so the breadcrumb (and its
    # folder tag) mounts.
    seed_committed_turn(session_id, prompt="ping", reply="pong")

    try:
        page.goto(f"{base_url}/c/{session_id}")

        # Unfiled → the tag reads "Add to project". Open it and pick the project.
        tag = page.get_by_test_id("header-project-tag")
        expect(tag).to_be_visible(timeout=30_000)
        expect(tag).to_have_attribute("aria-label", "Add to project")
        tag.click()
        page.get_by_role("menuitem", name=project_name).click()

        # It commits server-side…
        assert _await_filed_under(base_url, session_id) == project_id
        # …and the tag flips to the filed state (folder icon, project label).
        expect(page.get_by_test_id("header-project-tag")).to_have_attribute(
            "aria-label", f"Project: {project_name}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)


def test_header_folder_tag_creates_project_from_typed_name(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Typing an unmatched name offers "+ Create", filing into a new project."""
    base_url, session_id = seeded_session
    uniq = uuid.uuid4().hex[:6]
    project_name = f"E2E Header Create {uniq}"
    title = f"e2e-header-create-{uniq}"

    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()
    seed_committed_turn(session_id, prompt="ping", reply="pong")

    created_project_id: str | None = None
    try:
        page.goto(f"{base_url}/c/{session_id}")

        tag = page.get_by_test_id("header-project-tag")
        expect(tag).to_be_visible(timeout=30_000)
        tag.click()

        # Type a name no existing project matches → the create row appears.
        page.get_by_role("textbox", name="Search or create project").fill(project_name)
        create_row = page.get_by_role(
            "menuitem", name=re.compile(f"^Create {re.escape(project_name)}")
        )
        expect(create_row).to_be_visible()
        create_row.click()

        # The move creates the project on demand and files the session into it.
        created_project_id = _await_filed_under(base_url, session_id)
        assert created_project_id is not None
        projects = httpx.get(f"{base_url}/v1/sessions/projects", timeout=10.0).json()
        assert any(p["id"] == created_project_id and p["name"] == project_name for p in projects)
        expect(page.get_by_test_id("header-project-tag")).to_have_attribute(
            "aria-label", f"Project: {project_name}"
        )
    finally:
        if created_project_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/projects/{created_project_id}", timeout=10.0)
