"""E2E: with the runner offline but the host alive, the Files panel stays live.

When a session's runner process dies but the host that holds the workspace on
disk is still connected, the server reads the file panel (browse / changed
files / file content / diff) over the host tunnel instead of returning 503.
The web app must then keep the panel working — showing files and a passive
"Asleep — files shown live from the host" badge — rather than falling back to
the "agent is asleep, send a message to reconnect" dead-end.

The frontend cannot tell which side served the bytes; it decides purely from
the ``/health`` liveness (``runner_online`` / ``host_online``) plus whether the
filesystem queries succeed. So this drives the real SPA against a real
server-backed session (whose live runner actually serves the seeded files),
but patches the browser's *view* of liveness into the runner-offline /
host-online shape — exactly the state the host-served path produces in
production. Mirrors ``sessions/test_host_badge.py``'s liveness patching.

No LLM is involved: the file is seeded via the filesystem PUT endpoint.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

# Filesystem PUTs land in ``<repo-root>/<session_id>/`` (os_env.cwd: .), so
# clean that per-session dir up in teardown.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_FILE_PATH = "asleep_notes.txt"
_FILE_CONTENT = "Content served from the host while the agent is asleep."
_FAKE_HOST_ID = "host_offline_served"
# Unix seconds well before now so an offline runner is outside the startup
# grace and the session reads host-served (not "starting").
_OLD_CREATED_AT = 1_700_000_000


def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    """PUT a file into the session workspace via the filesystem API."""
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def seeded_offline_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed ``_FILE_PATH`` and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _FILE_PATH, _FILE_CONTENT)
    try:
        yield (base_url, session_id)
    finally:
        # Seeded files land under <repo-root>/<session_id>/ (os_env.cwd: .),
        # but a bare-name PUT can also land at the repo root — clean both.
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
        (_REPO_ROOT / _FILE_PATH).unlink(missing_ok=True)


def _patch_runner_offline_host_online(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` into runner-offline / host-online.

    The runner really is alive in the harness (so the filesystem endpoints
    return real data), but the SPA is told ``runner_online: false`` +
    ``host_online: true`` via ``/health`` — the exact liveness the host-served
    fallback produces in production. The snapshot is patched host-bound (with an
    old ``created_at`` + ``host_resumable``) so the chat view renders normally
    rather than entering a host-offline reconnect dead-end, and the sessions
    ``updates`` WS is blocked so a stream push can't revert liveness to the real
    (runner-online) values.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["host_id"] = _FAKE_HOST_ID
        payload["host_resumable"] = True
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        live = {"runner_online": False, "host_online": True}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot route registered last so it wins for /v1/sessions/{id}
    # (Playwright matches most-recently-registered first); /health falls
    # through via continue_() for anything it doesn't own.
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_offline_runner_host_served_files_panel_stays_live(
    page: Page,
    seeded_offline_file: tuple[str, str],
) -> None:
    """Runner offline + host online: the file list renders with the Asleep badge.

    Asserts the panel does NOT collapse to the reconnect dead-end: the seeded
    file appears in the All-files list, and the passive "Asleep" host-served
    badge is shown. A regression that re-gated the queries on runner liveness
    would leave the list empty and the badge absent.
    """
    base_url, session_id = seeded_offline_file
    _patch_runner_offline_host_online(page, session_id)

    page.goto(f"{base_url}/c/{session_id}?view=explore")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The passive host-served badge renders because the runner reads offline
    # while the host reads online — no wake-up, panel stays usable.
    expect(rail.get_by_test_id("files-host-served-badge")).to_be_visible(timeout=30_000)

    # The seeded file is listed even though the runner is (from the UI's view)
    # offline — proving the FS query fired and rendered host-served data.
    expect(rail.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).first).to_be_visible(
        timeout=30_000
    )


def test_offline_runner_host_served_file_content_opens(
    page: Page,
    seeded_offline_file: tuple[str, str],
) -> None:
    """Runner offline + host online: opening a file shows real content.

    This is the specific bug the fix addresses — the file-content viewer used
    to be gated on runner liveness, so opening a file while the agent was
    asleep left the viewer blank. Deep-links ``?file=`` to open the viewer and
    asserts the seeded body renders.
    """
    base_url, session_id = seeded_offline_file
    _patch_runner_offline_host_online(page, session_id)

    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    # Real body proves the content query fetched host-served bytes, not an
    # empty shell keyed off the path.
    expect(file_viewer.get_by_text(_FILE_CONTENT).first).to_be_visible(timeout=30_000)
