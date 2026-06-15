"""E2E: external comment mutations refresh an open CommentsPanel live.

Exercises the comments-freshness path end to end: comment mutations move
the per-session fingerprint (``comments_count`` / ``comments_updated_at``)
on the ``WS /v1/sessions/updates`` row, the SPA's
``SessionUpdatesProvider`` sees the fingerprint move in a ``changed``
frame and invalidates the ``["comments", <session>]`` query cache, and an
open CommentsPanel refetches — all without a reload.

The "other user / agent" side is simulated with direct REST calls
(``POST`` / ``PATCH /v1/sessions/{id}/comments``) — the same routes the
agent's ``update_comment`` tool and a collaborator's browser hit, so the
browser under test cannot know about the change except via the push
stream. No agent run is involved, keeping the test deterministic.

If this goes red, the likely regressions are:

- the session-list builders stopped folding the comments fingerprint
  into list items (``_comments_fingerprints_for`` in
  ``omnigent/server/routes/sessions.py``), or
- ``SessionUpdatesProvider`` stopped invalidating ``["comments", id]``
  on fingerprint movement (``syncCommentsFingerprints``), or
- the comment store stopped bumping ``updated_at`` on status changes
  (the PATCH test would then time out while the add test still passes,
  since adds also change the count).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_FILE_PATH = "realtime_comments.md"

# Anchor paragraph for the pre-seeded comment; must appear exactly once so
# the stored offsets unambiguously match the file content.
_ANCHOR_TEXT = "Realtime comment anchor paragraph."

_FILE_CONTENT = f"""\
# Realtime Comments Test

{_ANCHOR_TEXT}

Closing paragraph with filler text.
"""

# The WS /v1/sessions/updates rescan tick is 4 s in production; 15 s covers
# tick + refetch + render without masking a true stall.
_PUSH_TIMEOUT_MS = 15_000


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def commented_file_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a markdown file plus one open comment on it via REST.

    The file lands through the filesystem resources API (visible in the
    FileViewer without an agent run); the comment through
    ``POST /v1/sessions/{id}/comments`` with offsets matching the real
    file content so the SPA's anchor remap keeps it in place.

    :param seeded_session: Base fixture providing a runner-bound
        ``(base_url, session_id)`` pair.
    :returns: ``(base_url, session_id, seeded_comment_id)``.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    httpx.put(
        file_url,
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()

    start = _FILE_CONTENT.find(_ANCHOR_TEXT)
    assert start != -1, "fixture bug: anchor text missing from file content"
    comment_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": "Pre-seeded open comment.",
            "start_index": start,
            "end_index": start + len(_ANCHOR_TEXT),
            "anchor_content": _ANCHOR_TEXT,
        },
        timeout=10.0,
    )
    comment_resp.raise_for_status()
    yield (base_url, session_id, comment_resp.json()["id"])


def _open_comments_panel(page: Page, base_url: str, session_id: str) -> None:
    """Navigate to the session, open the seeded file, open CommentsPanel.

    :param page: Playwright page under test.
    :param base_url: Live server origin, e.g. ``"http://127.0.0.1:8000"``.
    :param session_id: Session to open, e.g. ``"conv_abc123"``.
    :returns: None. Leaves the page with the comments panel visible and
        the pre-seeded comment card rendered.
    """
    page.goto(f"{base_url}/c/{session_id}")
    # The rail defaults open but is remembered per session; ensure it is open so the changed-files
    # panel (and its file-open button) are reachable.
    open_right_rail(page)

    # The PUT-seeded file surfaces in the changed-files panel (it polls
    # the workspace endpoint); the open button carries the filename as
    # visible text, the sibling Download button only in its aria-label.
    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    file_viewer.get_by_role("button", name="Show comments").click()
    # The pre-seeded card proves the panel fetched the comment list once;
    # everything the test asserts afterwards must arrive via push.
    expect(file_viewer).to_contain_text("Pre-seeded open comment.", timeout=10_000)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_external_comment_add_appears_without_reload(
    page: Page,
    commented_file_session: tuple[str, str, str],
) -> None:
    """A comment POSTed outside the browser appears in the open panel.

    The count moving 1 → 2 is the fingerprint signal here. The page is
    never reloaded after the panel opens, so the new card can only
    render if the updates stream delivered the fingerprint change and
    the provider invalidated the comments cache.
    """
    base_url, session_id, _seeded_comment_id = commented_file_session
    _open_comments_panel(page, base_url, session_id)

    # Unique per run so the card locator can only match the comment
    # created below, never a leftover from a previous run.
    marker = f"external-comment-{uuid.uuid4().hex[:8]}"
    start = _FILE_CONTENT.find("Closing paragraph")
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": marker,
            "start_index": start,
            "end_index": start + len("Closing paragraph"),
            "anchor_content": "Closing paragraph",
        },
        timeout=10.0,
    ).raise_for_status()

    # KEY ASSERTION: the externally-added comment renders without a
    # reload. A timeout means the push → invalidate → refetch chain is
    # broken somewhere (see module docstring for the suspect list).
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_contain_text(marker, timeout=_PUSH_TIMEOUT_MS)


def test_external_status_change_clears_open_tab_without_reload(
    page: Page,
    commented_file_session: tuple[str, str, str],
) -> None:
    """A comment PATCHed to addressed leaves the Open tab live.

    This is the agent flow (``update_comment`` → draft → addressed) and
    the count-blind case: the row count stays 1, so only the
    ``updated_at`` bump can carry the change. The Open tab emptying
    without a reload proves edits — not just adds — propagate.
    """
    base_url, session_id, seeded_comment_id = commented_file_session
    _open_comments_panel(page, base_url, session_id)

    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}/comments/{seeded_comment_id}",
        json={"status": "addressed"},
        timeout=10.0,
    ).raise_for_status()

    # KEY ASSERTION: the Open tab drains live. A timeout here with the
    # add test green means in-place edits don't move the fingerprint
    # (updated_at not bumped, or the timestamp dropped from the frame).
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_contain_text("No open comments.", timeout=_PUSH_TIMEOUT_MS)
    # The card moved tabs rather than vanishing: the Addressed tab now
    # counts 1. (Tab label renders as "Addressed" + a count badge.)
    addressed_tab = file_viewer.get_by_role("button", name=re.compile("Addressed"))
    expect(addressed_tab).to_contain_text("1")
