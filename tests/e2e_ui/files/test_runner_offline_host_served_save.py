"""A .md edit must auto-save while the workspace is reachable (host-served).

The Files-panel editors used to gate auto-save on the SPA's raw runner-liveness
view. That view can read offline while the workspace is still reachable — the
host serves reads over its tunnel, and the offline signal can be stale after a
runner reconnect — and in that state the editor suppressed the save entirely:
the toolbar pill stuck at "Runner offline — your changes will save when it
reconnects" and the edit never reached the server until another prompt forced
a liveness resync.

This drives that exact state: the browser's view of the session is patched to
runner-offline / host-online (the runner in the harness stays alive, so reads
and writes genuinely land — mirroring ``files/test_offline_runner_host_served``),
the seeded .md renders, and a control PUT to a sibling path proves the
workspace accepts writes. No LLM is involved: files are seeded via the
filesystem PUT endpoint.

The test asserts the *desired* behavior — the edit auto-saves to the server —
so it fails while the save gate is keyed to raw runner liveness and passes once
the gate follows workspace reachability.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Filesystem PUTs land under ``<repo-root>/<session_id>/`` (os_env.cwd: .), so
# clean that per-session dir up in teardown.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_MD_PATH = "reconnect_notes.md"
_MD_CONTENT = """\
# Reconnect Notes

A paragraph that will be edited in the rich-text editor after reconnect.
"""

# A sibling path the control write targets to prove the workspace accepts
# writes while the SPA reads the runner as offline.
_CONTROL_PATH = "reconnect_control.txt"

_FAKE_HOST_ID = "host_offline_served"
# Unix seconds well before now so an offline runner is outside the startup
# grace and the session reads host-served (not "starting").
_OLD_CREATED_AT = 1_700_000_000

def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


def _read_file(base_url: str, session_id: str, path: str) -> str:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["content"]


def _wait_for_persisted(
    base_url: str,
    session_id: str,
    path: str,
    needle: str,
    timeout_s: float = 15.0,
) -> str:
    """Poll the file-content endpoint until ``needle`` lands (auto-save)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = _read_file(base_url, session_id, path)
        if needle in last:
            return last
        time.sleep(0.5)
    raise AssertionError(
        f"auto-save never persisted {needle!r} to {path}; last server content:\n{last}"
    )


@pytest.fixture
def seeded_offline_markdown(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _MD_PATH, _MD_CONTENT)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
        (_REPO_ROOT / _MD_PATH).unlink(missing_ok=True)
        (_REPO_ROOT / _CONTROL_PATH).unlink(missing_ok=True)


def _patch_runner_offline_host_online(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` into runner-offline / host-online.

    The runner is really alive in the harness (so reads and writes land), but
    the SPA is told ``runner_online: false`` + ``host_online: true`` via
    ``/health`` — the exact liveness the report's "runner offline (stale)"
    state presents. The snapshot is patched host-bound (old ``created_at`` +
    ``host_resumable``) so the chat view renders normally rather than a
    host-offline reconnect dead-end, and the sessions ``updates`` WS is blocked
    so a stream push can't revert liveness to the real (runner-online) values.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
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
        response = fetch_with_retry(route)
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

    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_markdown_edit_saves_while_host_serves_workspace(
    page: Page,
    seeded_offline_markdown: tuple[str, str],
) -> None:
    """With the runner reading offline but the host serving the workspace,
    a rich-text edit must reach the server without a prompt-forced resync.
    """
    base_url, session_id = seeded_offline_markdown
    _patch_runner_offline_host_online(page, session_id)

    page.goto(f"{base_url}/c/{session_id}?file={_MD_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=30_000)
    # Reads render under the runner-offline / host-online patch, proving the
    # workspace is reachable (host-served) — the state the report is about.
    expect(editor).to_contain_text("edited in the rich-text editor")

    # The workspace genuinely accepts writes in this liveness state, so a save
    # would land if the editor attempted it (rules out an unreachable runner).
    control_sentinel = "control-write-sentinel"
    _seed_file(base_url, session_id, _CONTROL_PATH, control_sentinel)
    assert control_sentinel in _read_file(base_url, session_id, _CONTROL_PATH)

    # Type an edit in the rich-text editor.
    sentinel = "reconnected-md-sentinel"
    editor.click()
    page.keyboard.press("Control+End")
    page.keyboard.type(f" {sentinel}")

    # Fail→pass target: the workspace is reachable, so the edit must auto-save
    # without the user sending a prompt to resync liveness. On the buggy build
    # the save is suppressed (stale runner-offline view) and this never lands.
    persisted = _wait_for_persisted(base_url, session_id, _MD_PATH, sentinel, timeout_s=15.0)
    assert sentinel in persisted
