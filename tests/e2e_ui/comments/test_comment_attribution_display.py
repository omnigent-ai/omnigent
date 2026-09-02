"""E2E: comment cards show a human identity, not a raw email, and flag edits.

Guards two user-facing polish gaps in the CommentsPanel's attribution row
(the footer under each comment card):

  1. ``test_comment_author_shows_display_name_not_email`` — a comment
     authored by a real (multi-user) identity must not render the raw email
     address as the visible author label. The reviewer-facing identity
     should be a human display name; today the card footer renders
     ``created_by`` (the email) verbatim.
  2. ``test_edited_comment_shows_edited_indicator`` — after a comment's
     body is edited (the author reopens it via the card's inline "Edit"
     affordance and saves a new body), the card must surface an "edited"
     marker so collaborators can tell the text changed. Today no such
     indicator exists anywhere on the card.

Both tests drive the browser AS the comment's author (via an
``X-Forwarded-Email`` context, mirroring ``test_comment_actions.py``'s
author-gated tests) so the multi-user attribution path — not the
single-user ``created_by = None`` carve-out — is what renders.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_FILE_PATH = "comment_identity.md"

# Anchor paragraph for the seeded comment; appears exactly once so the stored
# offsets unambiguously match the file content.
_ANCHOR_TEXT = "Comment identity anchor paragraph."

_FILE_CONTENT = f"""\
# Comment Identity Test

{_ANCHOR_TEXT}

Closing paragraph with filler text.
"""

# Bodies deliberately avoid the substring "edited" (any casing) so the
# edited-indicator assertion can never match the comment text itself.
_COMMENT_BODY = "Please tighten the wording in this paragraph."
_REVISED_BODY = "Please tighten the wording and split the long sentence."

# A real (non-``local``) identity: the comment's author and the browser's
# viewer. Attribution renders from ``created_by``, which the server records
# from this header on the seeding POST.
_AUTHOR_EMAIL = "sami.zayn@ui.test"

# Server-side LEVEL_EDIT — the minimum level the comments POST/PATCH requires.
_LEVEL_EDIT = 2


# ---------------------------------------------------------------------------
# Seeding helpers (mirroring tests/e2e_ui/comments/test_comment_actions.py)
# ---------------------------------------------------------------------------


def _grant_edit(base_url: str, session_id: str, user_id: str) -> None:
    """Grant ``user_id`` LEVEL_EDIT on the session via the permissions API.

    :param base_url: Live server origin.
    :param session_id: Session to grant access on.
    :param user_id: The identity to grant edit access to.
    """
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": user_id, "level": _LEVEL_EDIT},
        timeout=10.0,
    ).raise_for_status()


def _seed_file(base_url: str, session_id: str) -> None:
    """PUT the test markdown file into the session's filesystem resources."""
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    httpx.put(
        file_url,
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()


def _seed_comment(base_url: str, session_id: str, author: str) -> str:
    """POST one open comment anchored to ``_ANCHOR_TEXT`` and return its id.

    The ``author`` identity is sent as ``X-Forwarded-Email`` so the server
    records it as ``created_by`` — the field the card's attribution row
    renders from.

    :param base_url: Live server origin.
    :param session_id: Session to attach the comment to.
    :param author: Identity to attribute the comment to.
    :returns: The created comment id.
    """
    start = _FILE_CONTENT.find(_ANCHOR_TEXT)
    assert start != -1, "fixture bug: anchor missing from file content"
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": _COMMENT_BODY,
            "start_index": start,
            "end_index": start + len(_ANCHOR_TEXT),
            "anchor_content": _ANCHOR_TEXT,
        },
        headers={"X-Forwarded-Email": author},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def author_commented_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a file + one comment authored by ``_AUTHOR_EMAIL``, granted edit.

    The comment's ``created_by`` is a real email identity and the tests drive
    the browser as that same identity, so the multi-user attribution row (and
    the author-gated Edit affordance) render.

    :returns: ``(base_url, session_id, comment_id)``.
    """
    base_url, session_id = seeded_session
    _grant_edit(base_url, session_id, _AUTHOR_EMAIL)
    _seed_file(base_url, session_id)
    comment_id = _seed_comment(base_url, session_id, _AUTHOR_EMAIL)
    yield (base_url, session_id, comment_id)


def _author_context(browser: Browser) -> BrowserContext:
    """New context authenticated as ``_AUTHOR_EMAIL``.

    Manual contexts bypass both pytest-playwright's ``--video`` and the
    conftest's async-API recording patch, so honor
    ``OMNIGENT_E2E_RECORD_DIR`` explicitly to keep the journey filmable.

    :param browser: The pytest-playwright browser.
    :returns: A context whose every request carries ``X-Forwarded-Email``.
    """
    kwargs: dict[str, object] = {
        "extra_http_headers": {"X-Forwarded-Email": _AUTHOR_EMAIL},
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs["record_video_dir"] = record_dir
    return browser.new_context(**kwargs)


def _open_comments_panel(page: Page, base_url: str, session_id: str) -> Locator:
    """Navigate to the session, open the seeded file, open the CommentsPanel.

    :param page: Playwright page under test.
    :param base_url: Live server origin.
    :param session_id: Session to open.
    :returns: The visible FileViewer locator with the comments panel open.
    """
    page.goto(f"{base_url}/c/{session_id}")
    # The rail defaults open but is remembered per session; ensure it is open
    # so the changed-files panel (and its file-open button) are reachable.
    open_right_rail(page)

    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    file_viewer.get_by_role("button", name="Show comments").click()
    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()
    return file_viewer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_comment_author_shows_display_name_not_email(
    browser: Browser,
    author_commented_session: tuple[str, str, str],
) -> None:
    """The comment card's author label is a display name, not the raw email.

    The card may still expose the email in a hover tooltip (rendered in a
    portal outside the card), but the always-visible attribution text must
    not be the bare address — reviewers should see a human name.
    """
    base_url, session_id, _comment_id = author_commented_session

    ctx = _author_context(browser)
    try:
        page = ctx.new_page()
        file_viewer = _open_comments_panel(page, base_url, session_id)

        card = file_viewer.locator("div.rounded-lg.border").filter(has_text=_COMMENT_BODY)
        expect(card).to_be_visible(timeout=10_000)
        # Let the rendered attribution row settle on screen (also gives any
        # journey recording a beat on the buggy state).
        page.wait_for_timeout(1_500)

        # The bug: the footer renders ``created_by`` verbatim, so the raw
        # email address is part of the card's visible text.
        expect(card).not_to_contain_text(_AUTHOR_EMAIL, timeout=10_000)
    finally:
        ctx.close()


def test_edited_comment_shows_edited_indicator(
    browser: Browser,
    author_commented_session: tuple[str, str, str],
) -> None:
    """After an inline body edit, the card surfaces an "edited" marker.

    Drives the real edit journey as the comment's author (Edit → retype →
    Save) and then requires some visible "edited" indication on the comment
    card. The seeded and revised bodies never contain the substring
    "edited", and the assertion is scoped to the card — not the whole file
    viewer, whose toolbar renders two adjacent "Edit" labels that
    concatenate to "EditEdit" and false-match /edited/i — so it can only be
    satisfied by a real indicator.
    """
    base_url, session_id, _comment_id = author_commented_session

    ctx = _author_context(browser)
    try:
        page = ctx.new_page()
        file_viewer = _open_comments_panel(page, base_url, session_id)

        expect(file_viewer).to_contain_text(_COMMENT_BODY)
        # exact=True so this matches only the comment card's "Edit" button,
        # not the markdown toolbar's "View mode: Edit" dropdown trigger.
        file_viewer.get_by_role("button", name="Edit", exact=True).click()

        edit_textarea = file_viewer.locator("textarea")
        expect(edit_textarea).to_have_value(_COMMENT_BODY)
        edit_textarea.fill(_REVISED_BODY)
        # exact=True so this doesn't also match the markdown editor's
        # "All changes saved" status chip (its name contains "saved").
        file_viewer.get_by_role("button", name="Save", exact=True).click()

        # The edit persisted and the card re-rendered with the new body.
        card = file_viewer.locator("div.rounded-lg.border").filter(has_text=_REVISED_BODY)
        expect(card).to_be_visible(timeout=10_000)
        expect(file_viewer).not_to_contain_text(_COMMENT_BODY)
        page.wait_for_timeout(1_500)

        # The bug: nothing on the card indicates the comment was edited.
        # Scoped to the card so the toolbar's adjacent "Edit" labels
        # (concatenating to "EditEdit") can never satisfy the match.
        expect(card).to_contain_text(re.compile(r"edited", re.IGNORECASE), timeout=10_000)
    finally:
        ctx.close()
