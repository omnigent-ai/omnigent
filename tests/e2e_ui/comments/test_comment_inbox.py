"""E2E: a file comment surfaces in the Inbox until opened in the file browser.

Journey under test (the comment side of ``/inbox``):

1. Alice (identified via ``X-Forwarded-Email``) leaves a comment on a
   session file through ``POST /v1/sessions/{id}/comments`` — the same
   route her browser would hit.
2. The viewer's browser (no header ⇒ the ``local`` identity) opens
   ``/inbox``: the comment is listed, iconed with Alice's avatar pill
   (initials derived from her email), and the sidebar Inbox badge
   counts it.
3. Opening the file with the comments panel COLLAPSED (a plain
   ``?file=`` visit) does NOT clear the item — the comment hasn't
   actually been read yet.
4. "Open file" deep-links into the file browser (``?file=`` +
   ``?comment=``, which auto-opens the comments panel); the FileViewer
   marks the comment seen while the panel is open.
5. Back on ``/inbox``, the comment is gone and the empty state shows —
   the seen registry (localStorage) persists across the navigation.

If this goes red, the likely regressions are:

- ``useCommentInbox`` stopped assembling items from session rows'
  ``comments_count`` fingerprints (the inbox never lists the comment),
- the FileViewer's mark-seen effect lost its panel-open gate (step 3
  fails: the item clears from a mere file open) or stopped recording
  comments at all (step 5 fails: the item never clears), or
- the inbox's "Open file" deep link or AppShell's ``?file=`` restore
  broke (the file viewer never opens, so the comment is never seen).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# Dotted local part so the avatar pill derives two initials ("AS") —
# pinning that the pill renders from the author email, not a default.
_ALICE = "alice.smith@ui.test"

# Server-side LEVEL_EDIT — the minimum level the comments POST requires.
_LEVEL_EDIT = 2

_FILE_PATH = "inbox_comment_notes.md"

# Anchor paragraph for the seeded comment; appears exactly once so the
# stored offsets unambiguously match the file content.
_ANCHOR_TEXT = "Inbox comment anchor paragraph."

_FILE_CONTENT = f"""\
# Comment Inbox Test

{_ANCHOR_TEXT}

Closing paragraph with filler text.
"""

# Comment body asserted in the browser. Distinctive enough that only
# this test's comment card can match it.
_COMMENT_BODY = "Please tighten this paragraph (comment-inbox e2e)."


@pytest.fixture
def alice_commented_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a file plus one open comment authored by Alice via REST.

    The file lands through the filesystem resources API (no headers —
    the ``local`` user owns the session); the comment carries Alice's
    ``X-Forwarded-Email`` so the server records her as ``created_by``,
    exactly like a collaborator's browser would.

    :param seeded_session: Base fixture providing a runner-bound
        ``(base_url, session_id)`` pair.
    :returns: ``(base_url, session_id, comment_id)``.
    """
    base_url, session_id = seeded_session
    # Grant Alice edit access as the "local" owner (no header) — the
    # comments POST requires LEVEL_EDIT, and a stranger identity gets a
    # 404 on the session lookup otherwise.
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": _ALICE, "level": _LEVEL_EDIT},
        timeout=10.0,
    ).raise_for_status()
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
            "body": _COMMENT_BODY,
            "start_index": start,
            "end_index": start + len(_ANCHOR_TEXT),
            "anchor_content": _ANCHOR_TEXT,
        },
        headers={"X-Forwarded-Email": _ALICE},
        timeout=10.0,
    )
    comment_resp.raise_for_status()
    yield (base_url, session_id, comment_resp.json()["id"])


def test_comment_surfaces_in_inbox_until_opened_in_file_browser(
    page: Page,
    alice_commented_session: tuple[str, str, str],
) -> None:
    """Alice's comment shows in the inbox, then clears once its file is opened."""
    base_url, session_id, _comment_id = alice_commented_session

    page.goto(f"{base_url}/inbox")

    # The inbox lists the comment: rows carry comments_count, the page
    # fetches the session's comments, and the unseen draft survives the
    # collect filters (status/seen/author).
    item = page.locator('[data-testid="inbox-comment"]').filter(has_text=_COMMENT_BODY)
    expect(item).to_be_visible(timeout=15_000)

    # The item's icon is the author's avatar pill — initials derived
    # from Alice's email local part ("alice.smith" → "AS"), alongside
    # her identity as the author label.
    expect(item).to_contain_text("AS")
    expect(item).to_contain_text(_ALICE)

    # The sidebar Inbox badge counts the unseen comment. Exactly one
    # item exists on this worker's server (sessions are per-test and
    # torn down), so the singular label is deterministic.
    expect(page.get_by_label("1 inbox item waiting")).to_be_visible()

    # Opening the file WITHOUT the comment param leaves the comments
    # panel collapsed — the comment body is not on screen, so it must
    # NOT be marked seen and must still be listed on the inbox. If the
    # item is missing here, the mark-seen effect lost its panel-open
    # gate (it cleared from a mere file open).
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    expect(file_viewer).not_to_contain_text(_COMMENT_BODY)
    page.goto(f"{base_url}/inbox")
    item = page.locator('[data-testid="inbox-comment"]').filter(has_text=_COMMENT_BODY)
    expect(item).to_be_visible(timeout=15_000)

    # Deep-link into the file browser. The viewer opens the file, the
    # linked comment auto-opens the comments panel, and the panel
    # being open is what marks it seen.
    item.get_by_role("link", name="Open file").click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    expect(file_viewer).to_contain_text(_COMMENT_BODY, timeout=10_000)

    # Back on the inbox: the comment was seen, so it no longer lists,
    # the badge is gone, and the empty state renders. If this still
    # shows the item, the FileViewer never recorded it as seen.
    page.goto(f"{base_url}/inbox")
    expect(page.get_by_text("Nothing waiting on you")).to_be_visible(timeout=15_000)
    expect(page.locator('[data-testid="inbox-comment"]')).to_have_count(0)
    expect(page.get_by_label("1 inbox item waiting")).to_have_count(0)


@pytest.fixture
def self_commented_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a file plus one comment authored by the viewer themselves.

    Unlike :func:`alice_commented_session`, the comment POST carries no
    ``X-Forwarded-Email`` header, so the server records it as the
    ``local`` identity — stored with ``created_by = null``, exactly like a
    single-user deployment or a private session the viewer owns. The inbox
    should never surface such a comment: it only lists comments an
    identifiable *other* person left.

    :param seeded_session: Base fixture providing a runner-bound
        ``(base_url, session_id)`` pair.
    :returns: ``(base_url, session_id, comment_id)``.
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
    # No X-Forwarded-Email: the "local" owner authors the comment, so the
    # server stores created_by = null (attribution_user drops the sentinel).
    comment_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": _COMMENT_BODY,
            "start_index": start,
            "end_index": start + len(_ANCHOR_TEXT),
            "anchor_content": _ANCHOR_TEXT,
        },
        timeout=10.0,
    )
    comment_resp.raise_for_status()
    yield (base_url, session_id, comment_resp.json()["id"])


def test_own_comment_never_surfaces_in_inbox(
    page: Page,
    self_commented_session: tuple[str, str, str],
) -> None:
    """A comment the viewer authored themselves stays out of the inbox.

    The inbox is "comments other people left you". A comment stored with
    ``created_by = null`` (single-user / private session) is your own, so
    it must never list — even though the session reports ``comments_count
    > 0`` and the draft is unseen. Guards the ``collectCommentInboxItems``
    author filter against regressing to showing self-authored comments.
    """
    base_url, _session_id, _comment_id = self_commented_session

    page.goto(f"{base_url}/inbox")

    # The empty state must render and no comment card may appear, despite
    # the session carrying an unseen draft comment. A visible card here
    # means the author filter regressed (self / unauthored comments leak).
    expect(page.get_by_text("Nothing waiting on you")).to_be_visible(timeout=15_000)
    expect(page.locator('[data-testid="inbox-comment"]')).to_have_count(0)
    expect(page.get_by_label("1 inbox item waiting")).to_have_count(0)
