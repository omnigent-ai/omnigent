"""UI: the Share-project modal and Leave-project flow.

Projects are implicit collections of sessions carrying the same
``omni_project`` label; "sharing a project" fans one grant out across every
chat in it (see ``omnigent/server/routes/sessions.py`` project-share
endpoints). This drives the new sidebar surfaces end to end:

- ``ShareProjectModal.tsx`` — opened from the project folder's kebab
  (``data-testid="share-project"``): the **Share with all members**
  (``__members__``) and **Anyone with the link** (``__public__``) toggles, the
  invite-by-user form, the member list, and per-member revoke — each pinned
  against the project's ``/share`` and ``/members`` REST state so a
  silently-broken control can't pass.
- The folder kebab's **Leave project** item, which a member (not the owner)
  uses to drop their own access (``DELETE …/membership``).

The share-modal test runs as the single headerless ``local`` owner (same as
the per-chat ``test_permissions_modal``). The leave test adds a second
header-identified browser identity, mirroring ``test_sharing_journey``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
from playwright.sync_api import Browser, Locator, Page, expect

_MEMBERS_USER = "__members__"
_PUBLIC_USER = "__public__"
_LEVEL_READ = 1


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    """Poll *predicate* until truthy or the deadline passes.

    Modal mutations are optimistic (UI flip + background PUT/DELETE), so a REST
    read-back can beat the server commit; a short poll closes that race.
    """
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


def _set_project(base_url: str, session_id: str, project: str) -> None:
    """File a session under *project* via the label PATCH path."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omni_project": project}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _share_status(base_url: str, project: str, **headers: str) -> dict[str, object]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/projects/{project}/share", headers=headers, timeout=10.0
    )
    resp.raise_for_status()
    return resp.json()


def _members(base_url: str, project: str) -> dict[str, int]:
    resp = httpx.get(f"{base_url}/v1/sessions/projects/{project}/members", timeout=10.0)
    resp.raise_for_status()
    return {m["user_id"]: m["level"] for m in resp.json()}


def _projects_for(base_url: str, **headers: str) -> list[str]:
    resp = httpx.get(f"{base_url}/v1/sessions/projects", headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _section(page: Page, title: str) -> Locator:
    """The sidebar ``<section>`` whose collapse-header button reads *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _open_project_menu(page: Page, project: str) -> None:
    """Hover the project folder header and open its kebab menu."""
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible(timeout=30_000)
    header.hover()
    _section(page, project).get_by_test_id("project-actions").click()


def test_share_project_modal_drives_server_state(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Members/public toggles, invite and revoke all drive ``/share``+``/members``.

    Walks the whole modal surface in one project so each control is pinned
    against the REST state it mutates.
    """
    base_url, session_id = seeded_session
    project = f"ProjShare{uuid.uuid4().hex[:8]}"
    grantee = "alice@ui.test"
    _set_project(base_url, session_id, project)

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_menu(page, project)
    page.get_by_test_id("share-project").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Share project")).to_be_visible()

    # ── Share with all members: off → on creates a __members__ grant ─
    members_toggle = dialog.get_by_test_id("project-members-toggle")
    expect(members_toggle).not_to_be_checked()
    members_toggle.click()
    expect(members_toggle).to_be_checked()
    _wait_for(lambda: _share_status(base_url, project)["members"] is True)
    assert _members(base_url, project).get(_MEMBERS_USER) == _LEVEL_READ

    # ── Anyone with the link: off → on creates a __public__ grant ────
    public_toggle = dialog.get_by_test_id("project-public-toggle")
    expect(public_toggle).not_to_be_checked()
    public_toggle.click()
    expect(public_toggle).to_be_checked()
    _wait_for(lambda: _share_status(base_url, project)["public"] is True)

    # ── Invite a user at Read across the whole project ───────────────
    dialog.get_by_placeholder("alice@example.com").fill(grantee)
    dialog.get_by_role("button", name="Grant").click()
    expect(dialog.get_by_test_id("project-member-row").filter(has_text=grantee)).to_be_visible()
    _wait_for(lambda: _members(base_url, project).get(grantee) == _LEVEL_READ)

    # ── Revoke that user: row disappears, grant gone server-side ─────
    dialog.get_by_role("button", name=f"Remove {grantee}").click()
    expect(dialog.get_by_test_id("project-member-row").filter(has_text=grantee)).to_have_count(0)
    _wait_for(lambda: grantee not in _members(base_url, project))

    # ── Toggle members back off: grant is removed ────────────────────
    members_toggle.click()
    expect(members_toggle).not_to_be_checked()
    _wait_for(lambda: _members(base_url, project).get(_MEMBERS_USER) is None)


def test_member_can_leave_project(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A member the project was shared with can leave via the folder kebab.

    The owner (headerless ``local``) shares the project with Bob via the API;
    Bob — a header-identified second identity — opens the project folder kebab,
    which offers **Leave project** (not Share, since he can't manage), and
    leaving drops his grant so the project leaves his list.
    """
    base_url, session_id = seeded_session
    project = f"ProjLeave{uuid.uuid4().hex[:8]}"
    bob = f"bob-{uuid.uuid4().hex[:6]}@ui.test"
    _set_project(base_url, session_id, project)

    # Owner shares the project with Bob (read) via the API.
    resp = httpx.put(
        f"{base_url}/v1/sessions/projects/{project}/share",
        json={"user_id": bob, "level": _LEVEL_READ},
        timeout=10.0,
    )
    resp.raise_for_status()
    assert project in _projects_for(base_url, **{"X-Forwarded-Email": bob})

    # Bob's browser identity carries his email on every fetch/XHR.
    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": bob})
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        _open_project_menu(page, project)
        # Bob can't manage, so Share isn't offered — only Leave (+ Delete).
        expect(page.get_by_test_id("share-project")).to_have_count(0)
        page.get_by_test_id("leave-project").click()

        confirm = page.get_by_role("dialog")
        expect(confirm.get_by_text("Leave project?")).to_be_visible()
        confirm.get_by_role("button", name="Leave project").click()

        # Bob's grant is dropped, so the project leaves his list.
        _wait_for(lambda: project not in _projects_for(base_url, **{"X-Forwarded-Email": bob}))
    finally:
        ctx.close()
