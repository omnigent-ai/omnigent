"""Browser e2e: session delete must survive an artifact-store delete failure.

In deployed builds the artifact store can be a plugin whose ``delete``
talks to external infrastructure (a blob-storage sidecar), and a backend
failure there raised out of ``DELETE /v1/sessions/{id}``'s file cleanup
loop as an *unhandled* exception: ``_handle_unhandled_exception`` logged
it and the user got an opaque HTTP 500.

User journey:

  1. create a session
  2. attach a file to it (the session then owns an artifact-store blob)
  3. the artifact-store backend starts failing deletes (upstream fault)
  4. delete the session from the sidebar → the delete fails with an
     unhandled 500; the optimistic row snaps back with an error toast, and
     the session cannot be deleted while the backend fault persists — even
     though its file metadata rows were already destroyed (the row-delete
     runs before the blob-delete).

The backend fault is injected environmentally, without touching product
code: the uploaded blob's path in the spawned server's ``LocalArtifactStore``
root is replaced with a non-empty directory, so the store's
``path.unlink()`` raises ``OSError`` exactly where a deployed plugin's
``delete`` raises — a stand-in for an upstream blob-backend HTTP 500.

Expected (fixed) behavior asserted here: session file blob cleanup is
best-effort, like every other cleanup step in the same handler (runner
teardown, terminal cleanup, worktree removal are all explicitly
best-effort). A blob the backend cannot delete right now may leak and be
reaped later; it must not make the session undeletable or surface as an
unhandled 500. So: the DELETE succeeds, the row stays gone, and the
conversation row is durably removed (GET → 404).

On a build without best-effort blob cleanup this test fails at the
DELETE-status assertion with the unhandled-500 ``internal_error`` body.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import _server_state


def _artifact_root() -> Path:
    """Locate the spawned server's ``LocalArtifactStore`` root directory.

    The ``live_server`` fixture puts the SQLite DB and the artifact dir in
    the same per-session tmp dir (``--database-uri sqlite:///{tmp}/test.db``,
    ``--artifact-location {tmp}/artifacts``), and publishes the database URI
    in ``_server_state``. Derive the artifact root from it rather than
    guessing tmp paths.

    :returns: The artifact root directory of the spawned server.
    """
    database_uri = _server_state.get("database_uri")
    if not isinstance(database_uri, str) or not database_uri.startswith("sqlite:///"):
        # --ui-base-url runs point at an external server whose artifact dir
        # this test cannot reach to inject the backend fault.
        pytest.skip("requires the fixture-spawned server (no --ui-base-url)")
    root = Path(database_uri.removeprefix("sqlite:///")).parent / "artifacts"
    if not root.is_dir():
        pytest.skip(f"spawned server artifact dir not found: {root}")
    return root


def _server_unhandled_exception_tail(max_lines: int = 40) -> str:
    """Best-effort tail of the spawned server's last unhandled exception.

    The server subprocess inherits ``OMNIGENT_DATA_DIR`` from the pytest
    process and writes its log under ``logs/server/``. On failure this
    turns the opaque 500 body into the server-side traceback that names
    the raising frame. Returns ``""`` when the log can't be located.

    :param max_lines: Lines to include from the last occurrence onward.
    :returns: The traceback excerpt, or ``""``.
    """
    data_dir = os.environ.get("OMNIGENT_DATA_DIR", "")
    if not data_dir:
        return ""
    try:
        logs = sorted(
            Path(data_dir).glob("logs/server/server-*.log"),
            key=lambda p: p.stat().st_mtime,
        )
        if not logs:
            return ""
        lines = logs[-1].read_text(errors="replace").splitlines()
        starts = [i for i, line in enumerate(lines) if "Unhandled exception" in line]
        if not starts:
            return ""
        return "\n".join(lines[starts[-1] : starts[-1] + max_lines])
    except OSError:
        return ""


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _confirm_delete(page: Page, row: Locator) -> None:
    """Open *row*'s kebab and confirm the delete dialog.

    :param page: Playwright page.
    :param row: The sidebar ``<li>`` to delete.
    """
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("delete-conversation").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Delete", exact=True).click()


def test_delete_session_survives_artifact_store_delete_failure(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Deleting a session still succeeds when blob cleanup's backend fails.

    Failure modes this catches:

    - An artifact-store backend exception propagates out of the session
      delete as an unhandled HTTP 500 (``internal_error``), so the user's
      delete fails with an opaque toast and the session snaps back into
      the sidebar.
    - The failed delete leaves the session in a half-deleted state: its
      file rows were destroyed before the blob delete raised, but the
      conversation row survives, and every retry keeps failing while the
      backend fault persists.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = "artifact-delete-backend-fault"
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()

    # 1–2. The session owns an uploaded file, so the delete path below has
    # a blob to clean up.
    upload = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/files",
        files={"file": ("notes.txt", b"delete-fault repro attachment", "text/plain")},
        timeout=30.0,
    )
    upload.raise_for_status()
    file_id = upload.json()["id"]

    # 3. Backend fault: make the store's delete of this blob raise, the way
    # a deployed plugin's delete raises on an upstream 500. Replacing the
    # blob with a non-empty directory makes ``Path.unlink()`` raise OSError
    # deterministically (even for root, unlike a read-only parent dir).
    blob = _artifact_root() / file_id
    assert blob.is_file(), f"uploaded blob not found at {blob}"
    blob.unlink()
    blob.mkdir()
    (blob / "poison").write_text("makes unlink() raise on the blob path")

    try:
        page.goto(f"{base_url}/c/{session_id}")
        row = _row(page, session_id)
        expect(row).to_be_visible()

        # 4. Delete the session from the sidebar, capturing the DELETE's
        # real server response (the stop that precedes it is untouched).
        with page.expect_response(
            lambda r: r.request.method == "DELETE" and f"/v1/sessions/{session_id}" in r.url,
            timeout=60_000,
        ) as delete_info:
            _confirm_delete(page, row)
        delete_response = delete_info.value

        if delete_response.status != 200:
            # Bug reproduced. Let the user-facing failure state land on
            # screen (rollback toast + restored row) before failing, so a
            # recorded run ends on what the user sees.
            expect(page.get_by_test_id("toast")).to_be_visible()
            expect(row).to_be_visible()
            page.wait_for_timeout(1500)
            body: object
            try:
                body = delete_response.json()
            except Exception:
                body = delete_response.text()
            still_there = httpx.get(
                f"{base_url}/v1/sessions/{session_id}", timeout=10.0
            ).status_code
            log_tail = _server_unhandled_exception_tail()
            pytest.fail(
                "session delete must survive an artifact-store backend "
                f"failure, but DELETE /v1/sessions/{session_id} returned "
                f"{delete_response.status} {body!r} (session GET now "
                f"→ {still_there}); blob cleanup aborted the delete instead "
                "of degrading to best-effort"
                + (f"\n\nserver log:\n{log_tail}" if log_tail else "")
            )

        # Fixed behavior: the delete completed — the row stays gone (no
        # rollback), no failure toast, and the conversation row is durably
        # removed even though the blob could not be deleted.
        expect(row).to_have_count(0)
        expect(page.get_by_test_id("toast").filter(has_text="back in the sidebar")).to_have_count(
            0
        )
        deadline = time.monotonic() + 15.0
        last_status: int | None = None
        while time.monotonic() < deadline:
            last_status = httpx.get(
                f"{base_url}/v1/sessions/{session_id}", timeout=10.0
            ).status_code
            if last_status == 404:
                break
            time.sleep(0.25)
        assert last_status == 404, (
            f"deleted session should be gone from the store (404), got {last_status}"
        )
    finally:
        # Lift the injected fault so the fixture's teardown delete cannot
        # trip over it.
        if blob.is_dir():
            shutil.rmtree(blob, ignore_errors=True)
