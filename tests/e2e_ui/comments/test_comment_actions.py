"""E2E: per-comment actions in the CommentsPanel.

Covers the comment-card affordances that operate on already-saved comments,
independent of how the comment was anchored (so the cheap markdown-file +
REST-seeded-comment setup is used throughout — the panel UI is identical for
every file type):

  1. ``test_long_comment_show_more`` — a very long body collapses to a few
     lines with a "Show more" toggle; clicking it expands the body and the
     toggle flips to "Show less".
  2. ``test_edit_comment`` — the "Edit" action opens an inline editor; saving
     a new body updates the card and persists via REST.
  3. ``test_delete_comment`` — the "Delete" action removes the card and the
     comment is gone from the REST list.
  4. ``test_address_all_moves_comments_to_addressed_tab`` — "Address All"
     POSTs to ``/comments/send`` (which marks every open comment addressed and
     dispatches a formatted message to the agent); the Open tab drains and the
     comments reappear under the Addressed tab.
  5. ``test_comment_link_opens_same_comment_in_new_browser`` — the per-card
     "Copy link" button writes a deep link to the clipboard; opening that link
     in a fresh browser context lands on the file with the exact comment
     auto-selected (panel open, card highlighted).

The per-card Edit/Delete affordances are author-gated, with a deliberate
single-user carve-out:

  * Multi-user: they render only when the viewer authored the comment
    (``created_by === currentAuthorId``). The author-gated tests drive the
    browser AS a real identity (``X-Forwarded-Email``) and seed the comment
    authored by that same identity (see ``editor_commented_session``).
  * Single-user (the default local-dev experience): the e2e server runs in
    header mode with a single-user fallback, so a header-less POST records
    ``created_by = None`` (the server maps the ``"local"`` sentinel to None
    via ``attribution_user``). The client treats a null author as "owned by
    any editor", so Edit/Delete render without any identity header — see
    ``test_single_user_comment_is_editable_and_deletable``, which guards the
    plain local-dev path that header-driven tests would otherwise mask.

The tests that don't depend on author gating (show-more, address-all,
copy-link) use the cheaper header-less ``commented_session``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_FILE_PATH = "comment_actions.md"

# Anchor paragraph for the seeded comment(s); appears exactly once so the
# stored offsets unambiguously match the file content.
_ANCHOR_TEXT = "Comment actions anchor paragraph."

# A second anchor used only by the Address-All test (two open comments make the
# "all" in "Address All" meaningful).
_ANCHOR_TEXT_2 = "Comment actions second anchor."

_FILE_CONTENT = f"""\
# Comment Actions Test

{_ANCHOR_TEXT}

{_ANCHOR_TEXT_2}

Closing paragraph with filler text.
"""

# Distinctive comment bodies so each card locator matches only its own comment.
_SHORT_BODY = "Short open comment for actions test."

# A long body that overflows the 4-line clamp so the "Show more" toggle renders.
# whitespace-pre-wrap means the explicit newlines each become a rendered line.
_LONG_BODY = "\n".join(
    f"Long comment line number {i} with enough text to wrap." for i in range(12)
)

# Server-side LEVEL_EDIT — the minimum level the comments POST/PATCH requires.
_LEVEL_EDIT = 2

# A real (non-``local``) identity used by the author-gated edit/delete tests.
# In multi-user mode the per-card Edit/Delete affordances only render when the
# viewer authored the comment (``created_by === currentAuthorId``). To exercise
# that path we drive the browser AS this user (X-Forwarded-Email) and seed the
# comment authored by the same user, so the stored ``created_by`` matches the
# viewer. (The single-user path — header-less, ``created_by = None`` — is
# covered separately by ``test_single_user_comment_is_editable_and_deletable``.)
_EDITOR = "editor@ui.test"


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


def _seed_comment(
    base_url: str,
    session_id: str,
    anchor: str,
    body: str,
    *,
    author: str | None = None,
) -> str:
    """POST one open comment anchored to ``anchor`` and return its id.

    When ``author`` is given it is sent as ``X-Forwarded-Email`` so the server
    records that identity as ``created_by`` (required for the author-gated
    Edit/Delete affordances to render for a browser driven as the same user).
    With no ``author`` the server's single-user fallback maps the ``"local"``
    sentinel to ``created_by = None`` (via ``attribution_user``), which the
    client treats as "owned by any editor".

    :param base_url: Live server origin.
    :param session_id: Session to attach the comment to.
    :param anchor: Substring of the file content the comment anchors to.
    :param body: Comment body text.
    :param author: Optional identity to attribute the comment to.
    :returns: The created comment id.
    """
    start = _FILE_CONTENT.find(anchor)
    assert start != -1, f"fixture bug: anchor {anchor!r} missing from file content"
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": body,
            "start_index": start,
            "end_index": start + len(anchor),
            "anchor_content": anchor,
        },
        headers={"X-Forwarded-Email": author} if author else {},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def commented_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a markdown file plus one open comment, yield (base_url, session_id, comment_id).

    The comment is seeded without an author (``created_by = "local"``); this
    fixture serves the tests that do not depend on per-author edit/delete
    gating (show-more, address-all, copy-link).
    """
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id)
    comment_id = _seed_comment(base_url, session_id, _ANCHOR_TEXT, _SHORT_BODY)
    yield (base_url, session_id, comment_id)


@pytest.fixture
def editor_commented_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a file + one comment authored by ``_EDITOR``, who is granted edit.

    For the author-gated Edit/Delete tests: the comment's ``created_by`` is
    ``_EDITOR`` and the test drives the browser as ``_EDITOR`` (via an
    ``X-Forwarded-Email`` context), so the per-card Edit/Delete affordances
    render.

    :returns: ``(base_url, session_id, comment_id)``.
    """
    base_url, session_id = seeded_session
    _grant_edit(base_url, session_id, _EDITOR)
    _seed_file(base_url, session_id)
    comment_id = _seed_comment(base_url, session_id, _ANCHOR_TEXT, _SHORT_BODY, author=_EDITOR)
    yield (base_url, session_id, comment_id)


def _open_comments_panel(page: Page, base_url: str, session_id: str) -> object:
    """Navigate to the session, open the seeded file, open the CommentsPanel.

    :param page: Playwright page under test.
    :param base_url: Live server origin.
    :param session_id: Session to open.
    :returns: The visible FileViewer locator with the comments panel open.
    """
    page.goto(f"{base_url}/c/{session_id}")
    # The rail defaults open but is remembered per session; ensure it is open so
    # the changed-files panel (and its file-open button) are reachable.
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


def test_long_comment_show_more(
    page: Page,
    commented_session: tuple[str, str, str],
) -> None:
    """A long comment body clamps with a "Show more" toggle that expands it."""
    base_url, session_id, comment_id = commented_session
    # Replace the seeded short comment's body with a long one via REST so the
    # card overflows the line clamp.
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}/comments/{comment_id}",
        json={"body": _LONG_BODY},
        timeout=10.0,
    ).raise_for_status()

    file_viewer = _open_comments_panel(page, base_url, session_id)

    # The collapsed card overflows 4 lines, so the "Show more" toggle renders.
    show_more = file_viewer.get_by_role("button", name="Show more")
    expect(show_more).to_be_visible(timeout=10_000)

    # The body paragraph carries the line-clamp class while collapsed.
    body_para = file_viewer.locator("p.line-clamp-4").filter(has_text="Long comment line number 0")
    expect(body_para).to_be_visible()

    # Clicking the toggle expands the body: the clamp class is gone, the toggle
    # flips to "Show less", and a late line (clipped while collapsed) is shown.
    show_more.click()
    expect(file_viewer.get_by_role("button", name="Show less")).to_be_visible()
    expect(file_viewer.locator("p.line-clamp-4")).to_have_count(0)
    expect(file_viewer).to_contain_text("Long comment line number 11")


def test_edit_comment(
    browser: Browser,
    editor_commented_session: tuple[str, str, str],
) -> None:
    """Editing a comment updates the card and persists through the REST API.

    Driven as ``_EDITOR`` (the comment's author) via an ``X-Forwarded-Email``
    context, so the author-gated Edit affordance renders.
    """
    base_url, session_id, _comment_id = editor_commented_session

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": _EDITOR})
    try:
        page = ctx.new_page()
        file_viewer = _open_comments_panel(page, base_url, session_id)

        expect(file_viewer).to_contain_text(_SHORT_BODY)
        file_viewer.get_by_role("button", name="Edit").click()

        # The inline editor pre-fills the current body. It is the only textarea
        # on screen (the add-comment form requires an active selection, which we
        # don't have here).
        edit_textarea = file_viewer.locator("textarea")
        expect(edit_textarea).to_have_value(_SHORT_BODY)

        edited_body = "Edited comment body (e2e)."
        edit_textarea.fill(edited_body)
        # exact=True so this doesn't also match the markdown editor's
        # "All changes saved" status chip (its name contains "saved").
        file_viewer.get_by_role("button", name="Save", exact=True).click()

        # The card now shows the edited body and no longer the original.
        expect(file_viewer).to_contain_text(edited_body)
        expect(file_viewer).not_to_contain_text(_SHORT_BODY)
    finally:
        ctx.close()

    # REST confirms the persisted body changed.
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 1, f"Expected 1 comment, got {comments}"
    assert comments[0]["body"] == "Edited comment body (e2e)."


def test_delete_comment(
    browser: Browser,
    editor_commented_session: tuple[str, str, str],
) -> None:
    """Deleting a comment removes the card and the comment from the REST list.

    Driven as ``_EDITOR`` (the comment's author) via an ``X-Forwarded-Email``
    context, so the author-gated Delete affordance renders.
    """
    base_url, session_id, _comment_id = editor_commented_session

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": _EDITOR})
    try:
        page = ctx.new_page()
        file_viewer = _open_comments_panel(page, base_url, session_id)

        expect(file_viewer).to_contain_text(_SHORT_BODY)
        file_viewer.get_by_role("button", name="Delete").click()

        # The card disappears and the Open tab shows its empty state.
        expect(file_viewer).not_to_contain_text(_SHORT_BODY)
        expect(file_viewer).to_contain_text("No open comments.")
    finally:
        ctx.close()

    # REST confirms the comment is gone.
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    assert comments_resp.json() == [], "comment should be deleted server-side"


def test_single_user_comment_is_editable_and_deletable(
    page: Page,
    commented_session: tuple[str, str, str],
) -> None:
    """In single-user mode, a header-less comment shows Edit/Delete to any editor.

    This guards the default local-dev experience: there is no identity header,
    so the server records ``created_by = None`` (the ``"local"`` sentinel mapped
    via ``attribution_user``). The client treats a null author as "owned by any
    editor", so the per-card Edit and Delete affordances must render and work
    using the plain ``page`` fixture — no ``X-Forwarded-Email`` context.

    Regression guard: when the comments route stored the raw ``"local"``
    sentinel instead of ``None``, ``getCurrentAuthorId()`` (null for ``local``)
    never matched ``created_by``, so Edit/Delete silently vanished in local dev
    even though the author-gated tests (driven as a real identity) still passed.
    """
    base_url, session_id, _comment_id = commented_session

    # The server contract this test guards: a header-less POST records no
    # author. (Before the fix it stored the raw ``"local"`` sentinel here.)
    seeded = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    seeded.raise_for_status()
    assert seeded.json()[0]["created_by"] is None, seeded.json()

    file_viewer = _open_comments_panel(page, base_url, session_id)
    expect(file_viewer).to_contain_text(_SHORT_BODY)

    # Edit renders for the header-less viewer and persists.
    file_viewer.get_by_role("button", name="Edit").click()
    edit_textarea = file_viewer.locator("textarea")
    expect(edit_textarea).to_have_value(_SHORT_BODY)
    edited_body = "Edited in single-user mode (e2e)."
    edit_textarea.fill(edited_body)
    file_viewer.get_by_role("button", name="Save", exact=True).click()
    expect(file_viewer).to_contain_text(edited_body)
    expect(file_viewer).not_to_contain_text(_SHORT_BODY)

    # Delete also renders and removes the comment.
    file_viewer.get_by_role("button", name="Delete").click()
    expect(file_viewer).not_to_contain_text(edited_body)
    expect(file_viewer).to_contain_text("No open comments.")

    # REST confirms the comment is gone, and the seeded comment carried no
    # author (created_by None) — the single-user contract this test guards.
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    assert comments_resp.json() == [], "comment should be deleted server-side"


def test_address_all_moves_comments_to_addressed_tab(
    page: Page,
    commented_session: tuple[str, str, str],
) -> None:
    """ "Address All" sends open comments to the agent and moves them to Addressed.

    ``/comments/send`` marks every requested comment ``addressed`` server-side
    and returns a formatted message the SPA dispatches to the agent. The Open
    tab drains and the comments reappear under the Addressed tab — this is the
    deterministic, server-side half of the flow (the agent's reply is not
    asserted, only that the comments transitioned).
    """
    base_url, session_id, _comment_id = commented_session
    # Seed a second open comment so "Address All" addresses more than one.
    _seed_comment(base_url, session_id, _ANCHOR_TEXT_2, "Second open comment for address-all.")

    file_viewer = _open_comments_panel(page, base_url, session_id)

    # Both open comments are listed; the Open tab badge counts 2.
    expect(file_viewer).to_contain_text(_SHORT_BODY)
    expect(file_viewer).to_contain_text("Second open comment for address-all.")

    address_all = file_viewer.get_by_role("button", name=re.compile("Address All", re.IGNORECASE))
    expect(address_all).to_be_enabled()
    address_all.click()

    # The Open tab drains once /comments/send marks the comments addressed and
    # the comments query is invalidated + refetched.
    expect(file_viewer).to_contain_text("No open comments.", timeout=15_000)

    # The comments moved tabs rather than vanishing: the Addressed tab counts 2.
    addressed_tab = file_viewer.get_by_role("button", name=re.compile("Addressed"))
    expect(addressed_tab).to_contain_text("2")

    # Opening the Addressed tab shows the now-addressed comments.
    addressed_tab.click()
    expect(file_viewer).to_contain_text(_SHORT_BODY)
    expect(file_viewer).to_contain_text("Second open comment for address-all.")

    # REST confirms both comments carry status "addressed".
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 2, f"Expected 2 comments, got {comments}"
    assert all(c["status"] == "addressed" for c in comments), comments


def test_comment_link_opens_same_comment_in_new_browser(
    browser: Browser,
    commented_session: tuple[str, str, str],
) -> None:
    """The "Copy link" button yields a deep link that opens the exact comment.

    Clicking the per-card copy-link button writes ``window.location.href`` with
    a ``?comment={id}`` param to the clipboard (see ``copyCommentLink`` in
    FileViewer). Opening that URL in a FRESH browser context (no shared
    localStorage / seen-state) must auto-open the file, open the comments
    panel, and select that exact comment — the ``?comment=`` deep-link effect.

    Two dedicated contexts are used (rather than the shared ``page`` fixture):
    the first needs clipboard permissions to read what the button copied; the
    second is a clean "new browser" that only knows the copied URL.
    """
    base_url, session_id, comment_id = commented_session

    # First "browser": clipboard permissions so we can read the copied link.
    author_ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    try:
        page = author_ctx.new_page()
        file_viewer = _open_comments_panel(page, base_url, session_id)
        expect(file_viewer).to_contain_text(_SHORT_BODY)

        file_viewer.get_by_role("button", name="Copy link to comment").click()

        # navigator.clipboard.writeText resolves asynchronously; poll briefly
        # until the copied URL carries this comment's id.
        copied_url = ""
        for _ in range(30):
            copied_url = page.evaluate("() => navigator.clipboard.readText()")
            if f"comment={comment_id}" in copied_url:
                break
            page.wait_for_timeout(100)
        assert f"comment={comment_id}" in copied_url, (
            f"copied link {copied_url!r} is missing comment={comment_id}"
        )
        # The link also carries the file param so the deep link opens the file.
        assert f"file={_FILE_PATH}" in copied_url, (
            f"copied link {copied_url!r} is missing file={_FILE_PATH}"
        )
    finally:
        author_ctx.close()

    # Second "browser": a clean context that only knows the copied URL.
    visitor_ctx = browser.new_context()
    try:
        visitor = visitor_ctx.new_page()
        visitor.goto(copied_url)

        # The deep link opens the file and the comments panel automatically.
        new_viewer = visitor.locator('[data-testid="file-viewer"]:visible')
        expect(new_viewer).to_be_visible(timeout=30_000)
        expect(new_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible(
            timeout=15_000
        )
        expect(new_viewer).to_contain_text(_SHORT_BODY, timeout=15_000)

        # KEY ASSERTION: the linked comment is the SELECTED one — its card
        # carries the active (border-primary) styling that only the
        # ?comment=-applied activeSelection produces.
        selected_card = new_viewer.locator("div.border-primary").filter(has_text=_SHORT_BODY)
        expect(selected_card).to_be_visible(timeout=15_000)
    finally:
        visitor_ctx.close()
