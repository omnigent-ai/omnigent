"""E2E: an edit in a runner-asleep session must reconnect the runner and save.

A session whose runner went to sleep (``runner_online: false`` while its host
is up — the ``runner_asleep`` liveness) still lets the user open a file and
type. Editing is an unambiguous signal the user wants a live runner: the edit
must trigger a reconnect automatically and the buffered edit must save once
the runner is back — not strand the editor on the "Runner offline — your
changes will save when it reconnects" pill until the user manually sends a
chat message (today the only path that reconnects the runner).

Scope / honesty about the harness: the runner really is alive server-side —
only the browser's *view* of liveness is patched runner-offline via
``/health``, mirroring ``shells/test_new_shell_reconnects.py``. The health
patch is *stateful*: it reports the runner offline until the SPA issues any
mutating session-scoped request (the reconnect intent — an explicit wake call,
or the buffered save itself riding the server-side wake), then falls through
to the real (online) values, exactly as a woken runner registering would read.
What this pins is the user-visible contract: typing in an editor while the
session reads runner-asleep leads — with no further user action — to the edit
persisting server-side and the save pill settling. While the bug is live,
auto-save is fully suppressed while the runner reads offline (``saveDisabled``
in ``useEditorAutoSave``): the SPA never sends anything, the liveness view
never changes, the edit never saves, and this test fails at the persistence
wait with the save pill stuck at "Offline".
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

# Filesystem PUTs land in ``<repo-root>/<session_id>/`` (os_env.cwd: .), so
# clean that per-session dir up in teardown.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_MD_PATH = "asleep_edit_notes.md"
_MD_CONTENT = """\
# Asleep Edit Notes

A paragraph edited while the runner reads offline.
"""
_SENTINEL = "edit-while-asleep-sentinel-7b41"
# Unix seconds well before now so the runner-offline view escapes the fresh
# session startup grace (STARTING_GRACE_S) and classifies as runner_asleep.
_OLD_CREATED_AT = 1_700_000_000
# Accessible names of the markdown toolbar's auto-save pill (the button's
# title attribute, not the short visible label).
_OFFLINE_PILL = "Runner offline — your changes will save when it reconnects"
_SAVED_PILL = "All changes saved"


def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    """PUT a file into the session workspace via the filesystem API."""
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
    page: Page,
    base_url: str,
    session_id: str,
    path: str,
    needle: str,
    timeout_s: float = 45.0,
) -> str:
    """Poll the file-content endpoint until ``needle`` lands server-side.

    Generous timeout: a reconnect-then-flush needs one liveness poll interval
    (~10s) on top of the wake call and the debounced write itself. The wait
    between polls must be ``page.wait_for_timeout`` — in the sync Playwright
    API the stateful route handlers only run while the main thread is inside a
    Playwright call, so a plain ``time.sleep`` loop starves the ``/health``
    interception and the browser can never observe the reconnect it asked for.
    """
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = _read_file(base_url, session_id, path)
        if needle in last:
            return last
        page.wait_for_timeout(500)
    raise AssertionError(
        f"edit never persisted {needle!r} to {path} — the runner-asleep session "
        f"was not reconnected by the edit; last server content:\n{last}"
    )


@pytest.fixture
def seeded_markdown(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed ``_MD_PATH`` and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _MD_PATH, _MD_CONTENT)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
        (_REPO_ROOT / _MD_PATH).unlink(missing_ok=True)


def _patch_runner_asleep_until_reconnect(page: Page, session_id: str) -> dict[str, bool]:
    """Patch the browser's view of ``session_id`` into runner-asleep, statefully.

    ``runner_online: false`` + ``host_online: true`` via ``/health`` is the
    ``runner_asleep`` liveness — the wakeable state where the host relaunches
    the runner on demand. The snapshot gets an old ``created_at`` so the
    session escapes the startup grace (otherwise it reads ``starting``), and
    the sessions ``updates`` WS is blocked so a stream push can't revert the
    liveness to the real (online) values.

    The patch is *stateful*: a watcher route flags the first mutating
    session-scoped request the SPA makes (any verb but GET/HEAD/OPTIONS under
    ``/v1/sessions/{id}/``, excluding the automatic ``/read-state`` sync) as
    reconnect intent — an explicit wake call or the buffered save itself.
    From then on ``/health`` falls through to the real, runner-online values,
    exactly as a woken runner's tunnel registering would read. This keeps the
    test agnostic to *how* the client reconnects while still failing when it
    never tries. (If a fix reconnects via a non-session-scoped endpoint,
    extend the watcher.)

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    :returns: The shared state dict; ``state["woken"]`` flips on reconnect
        intent.
    """
    state = {"woken": False}

    def _watch_reconnect_intent(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and path.startswith(f"/v1/sessions/{session_id}/")
            and not path.endswith("/read-state")
        ):
            state["woken"] = True
        route.continue_()

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
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
        if state["woken"]:
            # Reconnect intent observed: report the real liveness (the runner
            # is genuinely online in this harness), like a woken runner.
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

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}/"), _watch_reconnect_intent)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)
    return state


def test_edit_in_asleep_session_reconnects_and_saves(
    page: Page,
    seeded_markdown: tuple[str, str],
) -> None:
    """Typing in a runner-asleep session's editor reconnects and saves.

    The UI believes the runner is offline (patched ``/health``), the user
    opens a seeded markdown file and types. The edit is an implicit reconnect
    request: with no further user action the runner must be reconnected and
    the buffered edit must persist server-side, with the save pill settling
    to saved. While the bug is live the editor stays stranded — auto-save is
    suppressed while the runner reads offline, no reconnect is ever kicked
    off, and the edit never reaches the server.
    """
    base_url, session_id = seeded_markdown
    _patch_runner_asleep_until_reconnect(page, session_id)

    try:
        page.goto(f"{base_url}/c/{session_id}?file={_MD_PATH}")

        file_viewer = page.locator('[data-testid="file-viewer"]:visible')
        expect(file_viewer).to_be_visible()
        editor = file_viewer.locator("[contenteditable='true']")
        expect(editor).to_be_visible(timeout=15_000)
        expect(editor).to_contain_text("A paragraph edited while the runner reads offline")

        # The patched liveness must take effect before the edit: the toolbar's
        # auto-save pill reads offline. Without this guard a broken patch would
        # leave the runner online and let plain auto-save pass the test
        # vacuously.
        expect(file_viewer.get_by_role("button", name=_OFFLINE_PILL)).to_be_visible(timeout=30_000)

        # The user edit: type a unique sentinel at the end of the document
        # while the session reads runner-asleep.
        editor.click()
        page.keyboard.press("Control+End")
        page.keyboard.type(f" {_SENTINEL}")

        # The edit itself must reconnect the runner and save — no chat message,
        # no manual retry. While the bug is live nothing is ever written and
        # this times out with the pill stuck at "Offline".
        persisted = _wait_for_persisted(page, base_url, session_id, _MD_PATH, _SENTINEL)
        assert _SENTINEL in persisted

        # And the editor's status settles once the reconnect lands.
        expect(file_viewer.get_by_role("button", name=_SAVED_PILL)).to_be_visible(timeout=30_000)
    finally:
        # Drop the stateful routes before the fixture closes the page so
        # in-flight /health polls don't error noisily during teardown.
        page.unroute_all(behavior="ignoreErrors")
