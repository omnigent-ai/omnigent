"""Browser e2e: double-clicking a project folder's name renames it in place.

Setup runs over REST before the page opens, so each recording starts on the sidebar.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import NamedTuple

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect


def _create_project(base_url: str, name: str) -> str:
    """Create a project over REST and return its id."""
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    response.raise_for_status()
    return str(response.json()["id"])


def _project_names(base_url: str) -> set[str]:
    """Return the names the server currently stores for the caller's projects."""
    response = httpx.get(f"{base_url}/v1/projects", timeout=10.0)
    response.raise_for_status()
    return {project["name"] for project in response.json()["data"]}


def _folder_header(page: Page, project: str) -> Locator:
    """Locate a project folder's header button by its visible name."""
    return page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=project)


class FiledProject(NamedTuple):
    base_url: str
    session_id: str
    name: str
    project_id: str


@pytest.fixture
def filed_project(seeded_session: tuple[str, str]) -> Iterator[FiledProject]:
    """File the seeded session into a fresh project."""
    base_url, session_id = seeded_session
    name = f"Roadmap {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, name)
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"project_id": project_id, "title": "Plan the launch"},
        timeout=10.0,
    ).raise_for_status()
    yield FiledProject(base_url, session_id, name, project_id)
    with contextlib.suppress(httpx.HTTPError):
        httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)


def _start_inline_rename(page: Page, project: str) -> Locator:
    """Double-click the folder's name and return the inline rename field."""
    header = _folder_header(page, project)
    expect(header).to_be_visible()
    header.get_by_text(project, exact=True).dblclick()
    name_input = page.get_by_test_id("rename-project-inline-input")
    expect(name_input).to_be_focused()
    expect(name_input).to_have_value(project)
    expect(name_input).to_have_js_property("selectionStart", 0)
    expect(name_input).to_have_js_property("selectionEnd", len(project))
    expect(page.get_by_role("dialog")).to_have_count(0)
    return name_input


def test_double_click_renames_project_in_place(
    request: pytest.FixtureRequest,
    filed_project: FiledProject,
) -> None:
    """Typing over the selected name and pressing Enter saves the new name."""
    base_url, session_id, project, _ = filed_project
    new_name = f"Launch {uuid.uuid4().hex[:6]}"
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    name_input = _start_inline_rename(page, project)
    page.keyboard.type(new_name)
    name_input.press("Enter")

    expect(name_input).to_have_count(0)
    expect(_folder_header(page, new_name)).to_be_visible()
    expect(_folder_header(page, project)).to_have_count(0)
    # The double-click's first click opened the project home. It follows the
    # rename only after the save lands, so the server reads below can't race it.
    expect(page.get_by_role("heading", name=new_name, level=1)).to_be_visible()
    expect(page.get_by_role("main").get_by_text("Plan the launch")).to_be_visible()
    assert new_name in _project_names(base_url)
    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    assert session["project_id"] == filed_project.project_id


def test_escape_keeps_the_project_name(
    request: pytest.FixtureRequest,
    filed_project: FiledProject,
) -> None:
    """Escape discards the typed name and sends no rename."""
    base_url, session_id, project, _ = filed_project
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    name_input = _start_inline_rename(page, project)
    page.keyboard.type("Discarded name")
    name_input.press("Escape")

    expect(name_input).to_have_count(0)
    expect(_folder_header(page, project)).to_be_visible()
    assert project in _project_names(base_url)
    assert "Discarded name" not in _project_names(base_url)


def test_rejected_name_keeps_the_old_name(
    request: pytest.FixtureRequest,
    filed_project: FiledProject,
) -> None:
    """A name another project already uses is refused with a toast."""
    base_url, session_id, project, _ = filed_project
    taken = f"Taken {uuid.uuid4().hex[:6]}"
    taken_id = _create_project(base_url, taken)
    try:
        page: Page = request.getfixturevalue("page")
        page.goto(f"{base_url}/c/{session_id}")
        expect(_folder_header(page, taken)).to_be_visible()

        name_input = _start_inline_rename(page, project)
        page.keyboard.type(taken)
        name_input.press("Enter")

        expect(page.get_by_text("Couldn't rename the project")).to_be_visible()
        expect(_folder_header(page, project)).to_be_visible()
        expect(_folder_header(page, taken)).to_have_count(1)
        assert project in _project_names(base_url)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/projects/{taken_id}", timeout=10.0)
