"""UI: the first-class project Share modal and Leave flow.

Projects are a first-class entity (`/v1/projects`); a chat filed under a
project inherits the project's ACL, so sharing a project covers every chat in
it. This drives the sidebar surfaces end to end:

- `ShareProjectModal.tsx` — opened from the project folder's kebab
  (`data-testid="share-project"`): the **Share with all members**
  (`__members__`) and **Anyone with the link** (`__public__`) toggles, the
  invite form, the member list, and per-member revoke — each pinned against the
  project's `/v1/projects/{id}/permissions` REST state.
- The folder kebab's **Leave project** item (`DELETE …/membership`).

The share-modal test runs as the single headerless ``local`` owner; the leave
test adds a second header-identified identity (mirrors `test_sharing_journey`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
from playwright.sync_api import Browser, Locator, Page, expect

_MEMBERS_USER = "__members__"
_PUBLIC_USER = "__public__"


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # transient httpx blip — retry until deadline
            last_exc = exc
        time.sleep(0.25)
    if last_exc is not None:
        raise last_exc
    raise AssertionError("condition not met within timeout")


def _create_project(base_url: str, name: str, **headers: str) -> str:
    resp = httpx.post(
        f"{base_url}/v1/projects", json={"name": name}, headers=headers, timeout=10.0
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _file_session(base_url: str, session_id: str, project_id: str, **headers: str) -> None:
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"project_id": project_id},
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()


def _share(base_url: str, project_id: str, user_id: str, level: int, **headers: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/projects/{project_id}/permissions",
        json={"user_id": user_id, "level": level},
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()


def _members(base_url: str, project_id: str, **headers: str) -> dict[str, int]:
    resp = httpx.get(
        f"{base_url}/v1/projects/{project_id}/permissions", headers=headers, timeout=10.0
    )
    resp.raise_for_status()
    return {m["user_id"]: m["level"] for m in resp.json()}


def _project_ids_for(base_url: str, **headers: str) -> set[str]:
    resp = httpx.get(f"{base_url}/v1/projects", headers=headers, timeout=10.0)
    resp.raise_for_status()
    return {p["id"] for p in resp.json()}


def _section(page: Page, title: str) -> Locator:
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _open_project_menu(page: Page, name: str) -> None:
    header = page.get_by_role("button", name=name, exact=True)
    expect(header).to_be_visible(timeout=30_000)
    header.hover()
    _section(page, name).get_by_test_id("project-actions").click()


def test_share_project_modal_drives_server_state(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Members/public toggles, invite and revoke all drive the project ACL."""
    base_url, session_id = seeded_session
    name = f"ProjShare{uuid.uuid4().hex[:8]}"
    grantee = "alice@ui.test"
    project_id = _create_project(base_url, name)
    _file_session(base_url, session_id, project_id)

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_menu(page, name)
    page.get_by_test_id("share-project").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Share project")).to_be_visible()

    members_toggle = dialog.get_by_test_id("project-members-toggle")
    expect(members_toggle).not_to_be_checked()
    members_toggle.click()
    expect(members_toggle).to_be_checked()
    _wait_for(lambda: _members(base_url, project_id).get(_MEMBERS_USER) == 1)

    public_toggle = dialog.get_by_test_id("project-public-toggle")
    public_toggle.click()
    expect(public_toggle).to_be_checked()
    _wait_for(lambda: _members(base_url, project_id).get(_PUBLIC_USER) == 1)

    dialog.get_by_placeholder("alice@example.com").fill(grantee)
    dialog.get_by_role("button", name="Grant").click()
    expect(dialog.get_by_test_id("project-member-row").filter(has_text=grantee)).to_be_visible()
    _wait_for(lambda: _members(base_url, project_id).get(grantee) == 1)

    dialog.get_by_role("button", name=f"Remove {grantee}").click()
    expect(dialog.get_by_test_id("project-member-row").filter(has_text=grantee)).to_have_count(0)
    _wait_for(lambda: grantee not in _members(base_url, project_id))


def test_member_can_leave_project(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A member the project was shared with can leave via the folder kebab."""
    base_url, session_id = seeded_session
    name = f"ProjLeave{uuid.uuid4().hex[:8]}"
    bob = f"bob-{uuid.uuid4().hex[:6]}@ui.test"
    project_id = _create_project(base_url, name)
    _file_session(base_url, session_id, project_id)
    _share(base_url, project_id, bob, 1)
    assert project_id in _project_ids_for(base_url, **{"X-Forwarded-Email": bob})

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": bob})
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        _open_project_menu(page, name)
        # Bob can't manage, so Share/Rename aren't offered — only Leave.
        expect(page.get_by_test_id("share-project")).to_have_count(0)
        page.get_by_test_id("leave-project").click()

        confirm = page.get_by_role("dialog")
        expect(confirm.get_by_text("Leave project?")).to_be_visible()
        confirm.get_by_role("button", name="Leave project").click()

        _wait_for(
            lambda: project_id not in _project_ids_for(base_url, **{"X-Forwarded-Email": bob})
        )
    finally:
        ctx.close()
