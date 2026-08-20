"""E2E: browsing outside the workspace is the session OWNER's privilege.

The files panel can navigate anywhere the session can reach. The workspace is
the session's shared context, so a collaborator granted the session sees it —
but everything past the workspace is the owner's own machine, and the
host-scoped filesystem endpoint behind the workspace picker is already
owner-scoped. Gating this route any lower would make a shared session a
weaker way to reach the very same files.

The route-level matrix lives in
``tests/server/routes/test_shell_permission_gate.py``, but it stubs the
permission store. This drives the real thing end to end: one live server, a
real session, a real ``PUT /v1/sessions/{id}/permissions`` grant, and two
browser contexts carrying different identities. If the wiring between a
genuinely shared session and the gate ever broke, the stubs would not notice
and this would.

The decisive assertions are per-identity: the owner's panel offers the
navigation control and lists a file that exists only OUTSIDE the workspace;
the collaborator is not offered the control at all, and the same request over
their own authenticated context is refused. Both layers are asserted on
purpose -- hiding the control is presentation, the 403 is the boundary.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect

from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
    open_right_rail,
)

_FAKE_HOST_ID = "host_browse_sharing"
# Strongest level short of ownership -- see the test docstring.
_LEVEL_EDIT = 2


@dataclass
class _SharedBrowse:
    """A runner-bound session plus an owner and a not-yet-granted collaborator."""

    session_id: str
    owner: httpx.Client
    bob: httpx.Client
    bob_email: str
    outside: Path


@pytest.fixture
def shared_browse(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
    tmp_path: Path,
) -> Iterator[_SharedBrowse]:
    """Owner-created, runner-bound session + a directory outside the workspace.

    Mirrors ``test_sharing_journey``'s fixture: the headerless client is the
    owner (the shared server is single-user for the default identity), and
    ``bob`` carries an ``X-Forwarded-Email`` so he resolves as a distinct
    user the permission store can grant or refuse.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    # No keep-alive: these clients idle for minutes while Playwright drives the
    # browser, and reusing a connection uvicorn already closed flakes.
    no_pool = httpx.Limits(max_keepalive_connections=0)
    owner = httpx.Client(base_url=live_server, timeout=30.0, limits=no_pool)
    bob = httpx.Client(
        base_url=live_server,
        headers={"X-Forwarded-Email": f"bob-{suffix}@ui.test"},
        timeout=30.0,
        limits=no_pool,
    )

    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    # Exists ONLY outside the workspace, so seeing it in the panel is proof the
    # tree really re-rooted rather than showing a workspace file of the same name.
    (outside / "owner-only.txt").write_text("visible only to the owner\n")

    create = owner.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    owner.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": str(_server_state["runner_id"])},
    ).raise_for_status()
    try:
        yield _SharedBrowse(
            session_id=session_id,
            owner=owner,
            bob=bob,
            bob_email=str(bob.headers["X-Forwarded-Email"]),
            outside=outside,
        )
    finally:
        owner.delete(f"/v1/sessions/{session_id}")
        owner.close()
        bob.close()
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _user_context(browser: Browser, email: str | None) -> BrowserContext:
    """A browser context whose requests carry *email* (or none, for the owner)."""
    if email is None:
        return browser.new_context()
    return browser.new_context(extra_http_headers={"X-Forwarded-Email": email})


def _stub_host_binding(page: Page, session_id: str, *, entry: Path) -> None:
    """Make the session look host-bound and offer *entry* in the picker.

    Only the HOST binding is faked -- the panel needs a host to point the
    directory browser at, and the e2e server has none. Reach, authorization
    and the listing are all the real server and runner, which is the point:
    the 403 under test is produced by the real permission check.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["host_id"] = _FAKE_HOST_ID
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"hosts": [{"host_id": _FAKE_HOST_ID, "name": "e2e-host", "status": "online"}]}
            ),
        )

    def _patch_host_fs(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "name": entry.name,
                            "path": str(entry),
                            "type": "directory",
                            "bytes": None,
                            "modified_at": None,
                        }
                    ],
                    "has_more": False,
                }
            ),
        )

    # Regex, not glob: these URLs carry query strings, which "**/path" misses.
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(rf"/v1/hosts/{re.escape(_FAKE_HOST_ID)}/filesystem"), _patch_host_fs)


def _open_files_tree(page: Page, base_url: str, session_id: str) -> None:
    """Navigate to the session and put the Files rail on the folder tree."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()


def _absolute_fs_url(base_url: str, session_id: str, target: Path) -> str:
    """The filesystem URL for an absolute path, in the wire form the web uses.

    Only the leading slash is percent-encoded; interior ones travel literally.
    """
    return (
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
        f"/filesystem/%2F{str(target).lstrip('/')}"
    )


def test_owner_browses_outside_workspace_but_shared_collaborator_cannot(
    browser: Browser,
    live_server: str,
    shared_browse: _SharedBrowse,
) -> None:
    """The owner navigates outside the workspace; a granted collaborator cannot.

    Both halves run against the same live session, so the only variable is
    identity. Bob is granted ``edit`` -- deliberately the strongest level
    short of ownership, so the test pins that even a full collaborator is
    refused, not merely a read-only viewer.
    """
    session_id = shared_browse.session_id
    outside = shared_browse.outside

    # ---- the owner can ----------------------------------------------------
    owner_ctx = _user_context(browser, None)
    owner_page = owner_ctx.new_page()
    try:
        _stub_host_binding(owner_page, session_id, entry=outside)
        _open_files_tree(owner_page, live_server, session_id)
        owner_rail = owner_page.get_by_role("complementary", name="Workspace")

        path_button = owner_rail.get_by_test_id("browse-location-path")
        expect(path_button).to_be_visible(timeout=30_000)
        path_button.click()
        picker = owner_page.get_by_test_id("workspace-picker")
        expect(picker).to_be_visible(timeout=15_000)
        picker.get_by_test_id(f"workspace-picker-entry-{outside.name}").click()

        # Proof the tree re-rooted: this file exists only outside the workspace.
        expect(owner_rail.get_by_text("owner-only.txt")).to_be_visible(timeout=30_000)
    finally:
        # The snapshot stub does a real `route.fetch()`, and `useSession`
        # refetches that URL for as long as the page lives. Closing with one in
        # flight leaks its error onto the next Playwright call — landing on an
        # unrelated later test — so drop the routes before closing.
        owner_page.unroute_all(behavior="ignoreErrors")
        owner_page.close()
        owner_ctx.close()

    # ---- share it, at the strongest non-owner level ------------------------
    grant = shared_browse.owner.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": shared_browse.bob_email, "level": _LEVEL_EDIT},
    )
    assert grant.status_code in (200, 201, 204), grant.text
    # Sanity: the grant really took, so a later 403 is the browse gate talking
    # and not simply "bob cannot see this session".
    assert shared_browse.bob.get(f"/v1/sessions/{session_id}").status_code == 200

    # ---- the collaborator cannot -----------------------------------------
    bob_ctx = _user_context(browser, shared_browse.bob_email)
    bob_page = bob_ctx.new_page()
    try:
        _stub_host_binding(bob_page, session_id, entry=outside)
        _open_files_tree(bob_page, live_server, session_id)
        bob_rail = bob_page.get_by_role("complementary", name="Workspace")

        # Positive signal first: bob's panel really rendered. Without this the
        # "control is absent" assertion below would also pass on a panel that
        # failed to load at all.
        expect(bob_rail.get_by_role("searchbox", name="Search all files")).to_be_visible(
            timeout=30_000
        )

        # The control is not offered at all. `reachable` is identical for every
        # viewer (it describes the ENVIRONMENT), so the panel must additionally
        # consult who is asking -- and a collaborator is not the owner. Showing
        # the control would open the owner-scoped host browser onto a 403.
        expect(bob_rail.get_by_test_id("browse-location-path")).to_have_count(0)
        expect(bob_rail.get_by_text("owner-only.txt")).to_have_count(0)

        refused = bob_page.request.get(_absolute_fs_url(live_server, session_id, outside))
        assert refused.status == 403, refused.text()
    finally:
        bob_page.unroute_all(behavior="ignoreErrors")
        bob_page.close()
        bob_ctx.close()

    # The owner is still allowed -- the gate discriminates on identity, it did
    # not simply break browsing for everyone once the session became shared.
    allowed = shared_browse.owner.get(
        _absolute_fs_url(live_server, session_id, outside).removeprefix(live_server)
    )
    assert allowed.status_code == 200, allowed.text


def test_shared_collaborator_double_clicks_into_a_workspace_folder(
    browser: Browser,
    live_server: str,
    shared_browse: _SharedBrowse,
    request: pytest.FixtureRequest,
) -> None:
    """A collaborator CAN open a folder inside the workspace by double-click.

    The counterpart to the test above, and the reason the panel sends an
    in-workspace location in RELATIVE form: relative is authorized at ordinary
    read level, absolute is owner-gated (the 403 pinned above). Sending the
    folder's absolute path would refuse a collaborator a folder already listed
    in front of them.
    """
    session_id = shared_browse.session_id

    env = shared_browse.owner.get(f"/v1/sessions/{session_id}/resources/environments/default")
    env.raise_for_status()
    root = Path(env.json()["metadata"]["root"])
    folder = root / "shared-subfolder"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "inside.txt").write_text("reachable without owner rights\n")
    # The workspace is a real checkout shared with every other test in the
    # shard, so this fixture directory must not outlive the test.
    request.addfinalizer(lambda: shutil.rmtree(folder, ignore_errors=True))

    grant = shared_browse.owner.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": shared_browse.bob_email, "level": _LEVEL_EDIT},
    )
    assert grant.status_code in (200, 201, 204), grant.text

    bob_ctx = _user_context(browser, shared_browse.bob_email)
    bob_page = bob_ctx.new_page()
    try:
        _open_files_tree(bob_page, live_server, session_id)
        bob_rail = bob_page.get_by_role("complementary", name="Workspace")

        row = bob_rail.get_by_role("button", name="shared-subfolder/", exact=True)
        expect(row).to_be_visible(timeout=30_000)
        row.dblclick()

        # Re-rooted for a non-owner: the folder's contents are the top level.
        expect(bob_rail.get_by_text("inside.txt")).to_be_visible(timeout=30_000)
        expect(row).to_have_count(0)
        # Still no way OUT of the workspace -- navigating in does not hand a
        # collaborator the owner-scoped browser.
        expect(bob_rail.get_by_test_id("browse-location-path")).to_have_count(0)
    finally:
        bob_page.close()
