"""UI: the comment-only session permission level end to end in a real browser.

A comment grant (level 5) ranks between Read and Edit: the collaborator can
read the session and write review comments, but can't message the agent,
change a comment's status, or touch comments they didn't write. The unit and
route tests pin each gate in isolation; this drives the SPA against a real
server so the UI's level handling, the share dialog, and the server gates are
exercised together.

Two kinds of server are used:

* Dedicated multi-user servers (``spawn_multi_user_server``) for the share
  dialog, because Share is hidden on the single-user ``live_server``. The
  ``comment_sharing`` release feature is toggled per server through
  ``OMNIGENT_FEATURES``.
* The shared runner-bound ``live_server`` for the commenter/reader journeys,
  which need a session with a workspace file. That server runs without
  ``comment_sharing``, so the comment grant is written straight into its store,
  the same shape as a grant made while the feature was on and kept after it was
  turned off. The commenter UI keys off the session's level, not the feature.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _server_state, open_right_rail

# Mirrors omnigent/server/auth.py.
_LEVEL_READ = 1
_LEVEL_COMMENT = 5

_COMMENT_SHARING_FEATURE = {"OMNIGENT_FEATURES": "comment_sharing"}

pytestmark = pytest.mark.min_server_version("0.17.0")


# ---------------------------------------------------------------------------
# Share dialog
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def comment_sharing_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A multi-user server with the ``comment_sharing`` feature on."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_comment_sharing_on")
    yield from spawn_multi_user_server(
        mock_llm_server_url, server_tmp, extra_server_env=_COMMENT_SHARING_FEATURE
    )


@pytest.fixture(scope="module")
def comment_sharing_read_only_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A multi-user server in read-only sharing mode with ``comment_sharing`` on."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_comment_sharing_read_only")
    yield from spawn_multi_user_server(
        mock_llm_server_url,
        server_tmp,
        extra_server_env={**_COMMENT_SHARING_FEATURE, "OMNIGENT_SHARING_MODE": "read_only"},
    )


@pytest.fixture(scope="module")
def comment_sharing_off_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[MultiUserServer, Path]]:
    """A multi-user server with ``comment_sharing`` off, plus its SQLite path.

    The feature is cleared explicitly so an ambient ``OMNIGENT_FEATURES`` can't
    turn it on.
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_comment_sharing_off")
    for server in spawn_multi_user_server(
        mock_llm_server_url, server_tmp, extra_server_env={"OMNIGENT_FEATURES": ""}
    ):
        yield server, server_tmp / "test.db"


def _admin_page(browser: Browser) -> Page:
    """A page whose requests carry the admin (session owner) identity."""
    context = browser.new_context(extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL})
    return context.new_page()


def _permissions(base_url: str, session_id: str) -> dict[str, int]:
    """The session's grants as ``{user_id: level}``, read as the admin.

    Advertises the comment level like the web client does; without it the
    server reports comment grants as read.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        headers={"X-Forwarded-Email": ADMIN_EMAIL, "X-Omnigent-Permission-Levels": "comment"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return {p["user_id"]: p["level"] for p in resp.json()["permissions"]}


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    """Poll *predicate* until truthy; the modal's mutations land asynchronously."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("condition not met within timeout")


def _open_share_modal(page: Page) -> Locator:
    """Open the Share modal from the chat header and return the dialog."""
    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_enabled(timeout=60_000)
    share.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Share this session")).to_be_visible()
    return dialog


def _new_grant_level_select(dialog: Locator) -> Locator:
    """The add-user form's level select (grant rows carry an aria-label)."""
    return dialog.locator("form [role='combobox']:not([aria-label])")


def _open_option_labels(page: Page) -> list[str]:
    """The labels of the currently open select's options, in display order."""
    options = page.get_by_role("option")
    expect(options.first).to_be_visible()
    return [label.strip() for label in options.all_inner_texts()]


def test_share_dialog_offers_and_grants_comment(
    browser: Browser,
    comment_sharing_server: MultiUserServer,
) -> None:
    """With ``comment_sharing`` on, the owner can grant Comment from the dialog.

    The option sits between Read and Edit, the grant lands server-side as level
    5, and the new grant row shows Comment, including after a reload.
    """
    server = comment_sharing_server
    grantee = f"commenter-{uuid.uuid4().hex[:6]}@ui.test"
    page = _admin_page(browser)
    page.goto(f"{server.public_url}/c/{server.session_id}")
    dialog = _open_share_modal(page)

    _new_grant_level_select(dialog).click()
    assert _open_option_labels(page) == ["Read", "Comment", "Edit"]
    page.get_by_role("option", name="Comment").click()

    dialog.get_by_placeholder("alice@example.com").fill(grantee)
    dialog.get_by_role("button", name="Grant").click()
    expect(dialog.get_by_title(grantee)).to_be_visible()
    _wait_for(lambda: _permissions(server.base_url, server.session_id).get(grantee) == 5)

    row_select = dialog.get_by_role("combobox", name=f"Permission level for {grantee}")
    expect(row_select).to_have_text("Comment")

    # The row's own select offers the same three levels, and Comment → Read
    # re-levels the grant.
    row_select.click()
    assert _open_option_labels(page) == ["Read", "Comment", "Edit"]
    page.get_by_role("option", name="Read").click()
    _wait_for(lambda: _permissions(server.base_url, server.session_id).get(grantee) == _LEVEL_READ)

    # Back to Comment, then reload: the persisted grant renders as Comment.
    row_select.click()
    page.get_by_role("option", name="Comment").click()
    _wait_for(
        lambda: _permissions(server.base_url, server.session_id).get(grantee) == _LEVEL_COMMENT
    )
    page.reload()
    dialog = _open_share_modal(page)
    expect(dialog.get_by_role("combobox", name=f"Permission level for {grantee}")).to_have_text(
        "Comment"
    )


def test_read_only_sharing_offers_read_and_comment_only(
    browser: Browser,
    comment_sharing_read_only_server: MultiUserServer,
) -> None:
    """Read-only sharing caps new grants at Read or Comment: Edit is hidden.

    Existing grants render as fixed labels in this mode, so the Comment grant
    shows as text rather than a select.
    """
    server = comment_sharing_read_only_server
    grantee = f"ro-commenter-{uuid.uuid4().hex[:6]}@ui.test"
    page = _admin_page(browser)
    page.goto(f"{server.public_url}/c/{server.session_id}")
    dialog = _open_share_modal(page)
    expect(dialog).to_contain_text("invite others to view or comment on this session")

    _new_grant_level_select(dialog).click()
    assert _open_option_labels(page) == ["Read", "Comment"]
    page.get_by_role("option", name="Comment").click()
    dialog.get_by_placeholder("alice@example.com").fill(grantee)
    dialog.get_by_role("button", name="Grant").click()
    _wait_for(
        lambda: _permissions(server.base_url, server.session_id).get(grantee) == _LEVEL_COMMENT
    )

    row = dialog.get_by_title(grantee).locator("xpath=..")
    expect(row).to_contain_text("Comment")
    expect(row.get_by_role("combobox")).to_have_count(0)


def test_comment_sharing_off_hides_option_but_renders_existing_grant(
    browser: Browser,
    comment_sharing_off_server: tuple[MultiUserServer, Path],
) -> None:
    """With ``comment_sharing`` off, Comment isn't offered for new grants.

    A comment grant made while the feature was on keeps rendering as Comment
    rather than falling back to a blank or wrong level.
    """
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    server, db_path = comment_sharing_off_server
    grantee = f"legacy-commenter-{uuid.uuid4().hex[:6]}@ui.test"
    # The grant route refuses level 5 on this server, so write it the way it
    # would already exist from before the feature was turned off.
    blocked = httpx.put(
        f"{server.base_url}/v1/sessions/{server.session_id}/permissions",
        json={"user_id": grantee, "level": _LEVEL_COMMENT},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=10.0,
    )
    assert blocked.status_code == 403, blocked.text
    SqlAlchemyPermissionStore(f"sqlite:///{db_path}").grant(
        grantee, server.session_id, _LEVEL_COMMENT
    )

    page = _admin_page(browser)
    page.goto(f"{server.public_url}/c/{server.session_id}")
    dialog = _open_share_modal(page)
    expect(dialog).to_contain_text("Invite others to view or collaborate on this session.")

    _new_grant_level_select(dialog).click()
    assert _open_option_labels(page) == ["Read", "Edit"]
    # Close the select by re-picking its current value; Escape can also
    # dismiss the dialog underneath.
    page.get_by_role("option", name="Read").click()
    expect(dialog).to_be_visible()

    row_select = dialog.get_by_role("combobox", name=f"Permission level for {grantee}")
    expect(row_select).to_have_text("Comment")


# ---------------------------------------------------------------------------
# Commenter and reader journeys
# ---------------------------------------------------------------------------

_FILE_PATH = "comment_level_target.py"
# Its own token on a short line so a double-click selects exactly this word.
_ANCHOR_WORD = "commentlevelanchor"
_FILE_CONTENT = f"""\
def review_me(value):
    # {_ANCHOR_WORD}
    return value * 2
"""
_OWNER_COMMENT = "Owner's unattributed note."
_COMMENT_ONLY_PLACEHOLDER = "You have comment-only access to this session"
_READ_ONLY_PLACEHOLDER = "You have read-only access to this session"
_ADD_COMMENT_BUTTON = re.compile("Add comment", re.IGNORECASE)


def _seed_comment_level_grant(session_id: str, user_id: str) -> None:
    """Write a comment grant straight into the shared server's store.

    ``live_server`` runs without ``comment_sharing``, so its grant route
    refuses level 5; seeding the store mirrors a grant kept after the feature
    was switched off.
    """
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    database_uri = _server_state.get("database_uri")
    if not database_uri:
        pytest.skip("needs the spawned server's database (unavailable with --ui-base-url)")
    SqlAlchemyPermissionStore(str(database_uri)).grant(user_id, session_id, _LEVEL_COMMENT)


@pytest.fixture
def reviewed_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """A runner-bound session with a Python file and one owner comment.

    The owner is the headerless ``local`` identity, so the seeded comment has
    no recorded author; only editors may edit or delete it. Workspace files
    are shared so collaborators below edit can open the file at all.
    """
    base_url, session_id = seeded_session
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"share_workspace_files": True},
        timeout=10.0,
    ).raise_for_status()
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()
    start = _FILE_CONTENT.index("return")
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": _OWNER_COMMENT,
            "start_index": start,
            "end_index": start + len("return"),
            "anchor_content": "return",
        },
        timeout=10.0,
    ).raise_for_status()
    yield base_url, session_id


def _user_context(browser: Browser, email: str | None) -> BrowserContext:
    """A browser context for *email*, or the headerless owner when ``None``."""
    if email is None:
        return browser.new_context()
    return browser.new_context(extra_http_headers={"X-Forwarded-Email": email})


def _open_file(
    page: Page, base_url: str, session_id: str, *, show_comments: bool = True
) -> Locator:
    """Open the seeded file in Monaco, optionally with the comments panel showing.

    Start a selection with the panel closed: with it already open, a
    double-click selects the word without surfacing the floating
    "Add comment" button (for every level, owner included).
    """
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer.locator(".view-lines")).to_contain_text(_ANCHOR_WORD, timeout=20_000)
    if show_comments:
        file_viewer.get_by_role("button", name="Show comments").click()
        expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()
        expect(file_viewer).to_contain_text(_OWNER_COMMENT)
    return file_viewer


def _comment_card(file_viewer: Locator, body: str) -> Locator:
    """The comment card whose body is *body*."""
    return file_viewer.locator("div.rounded-lg.border").filter(has_text=body)


def _comments(base_url: str, session_id: str) -> list[dict]:
    """The file's comments as the headerless owner sees them."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def test_commenter_can_review_but_not_drive_the_agent(
    browser: Browser,
    reviewed_session: tuple[str, str],
) -> None:
    """A comment-only collaborator writes and manages their own comments only.

    * The composer is locked with the comment-only placeholder and Send is
      disabled; the server refuses a message or interrupt as well.
    * Selecting code offers "Add comment"; the new comment is attributed to
      the commenter, and they can edit and delete it.
    * The owner's unattributed comment has no Edit/Delete for them, and
      "Address All" (which marks comments addressed and messages the agent) is
      disabled.
    * The owner sees the commenter's comment with their identity.
    """
    base_url, session_id = reviewed_session
    commenter = f"commenter-{uuid.uuid4().hex[:6]}@ui.test"
    _seed_comment_level_grant(session_id, commenter)
    body = f"Commenter note {uuid.uuid4().hex[:6]}"
    edited = f"{body} (edited)"

    ctx = _user_context(browser, commenter)
    try:
        page = ctx.new_page()
        file_viewer = _open_file(page, base_url, session_id, show_comments=False)

        composer = page.get_by_placeholder(_COMMENT_ONLY_PLACEHOLDER)
        expect(composer).to_be_visible(timeout=15_000)
        expect(composer).to_be_disabled()
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()

        file_viewer.get_by_text(_ANCHOR_WORD).first.dblclick()
        add_comment = page.get_by_role("button", name=_ADD_COMMENT_BUTTON)
        expect(add_comment).to_be_visible()
        add_comment.click()

        # "Add comment" opens the panel with the selection as the pending anchor.
        expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()
        expect(file_viewer).not_to_contain_text("You have read-only access to this session.")
        expect(file_viewer.get_by_role("button", name=re.compile("Address All"))).to_be_disabled()
        owner_card = _comment_card(file_viewer, _OWNER_COMMENT)
        expect(owner_card).to_be_visible()
        expect(owner_card.get_by_role("button", name="Edit", exact=True)).to_have_count(0)
        expect(owner_card.get_by_role("button", name="Delete")).to_have_count(0)

        textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
        expect(textarea).to_be_visible()
        textarea.fill(body)
        file_viewer.get_by_role("button", name="Add Comment").click()

        card = _comment_card(file_viewer, body)
        expect(card).to_be_visible()
        expect(card).to_contain_text(commenter)
        stored = {c["body"]: c for c in _comments(base_url, session_id)}
        assert stored[body]["created_by"] == commenter
        assert stored[body]["anchor_content"] == _ANCHOR_WORD
        assert stored[body]["status"] == "draft"

        card.get_by_role("button", name="Edit", exact=True).click()
        edit_box = card.locator("textarea")
        expect(edit_box).to_have_value(body)
        edit_box.fill(edited)
        card.get_by_role("button", name="Save", exact=True).click()
        expect(_comment_card(file_viewer, edited)).to_be_visible()
        _wait_for(lambda: edited in {c["body"] for c in _comments(base_url, session_id)})
    finally:
        ctx.close()

    # The server holds the line the UI draws.
    as_commenter = httpx.Client(
        base_url=base_url, headers={"X-Forwarded-Email": commenter}, timeout=10.0
    )
    try:
        for event in (
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
            {"type": "interrupt", "data": {}},
        ):
            resp = as_commenter.post(f"/v1/sessions/{session_id}/events", json=event)
            assert resp.status_code == 403, (event["type"], resp.text)
        comment_id = next(c["id"] for c in _comments(base_url, session_id) if c["body"] == edited)
        resp = as_commenter.patch(
            f"/v1/sessions/{session_id}/comments/{comment_id}", json={"status": "addressed"}
        )
        assert resp.status_code == 403, resp.text
        resp = as_commenter.post(
            f"/v1/sessions/{session_id}/comments/send", json={"comment_ids": [comment_id]}
        )
        assert resp.status_code == 403, resp.text
    finally:
        as_commenter.close()

    # The owner sees the comment attributed to the commenter.
    owner_ctx = _user_context(browser, None)
    try:
        owner_page = owner_ctx.new_page()
        owner_viewer = _open_file(owner_page, base_url, session_id)
        expect(_comment_card(owner_viewer, edited)).to_contain_text(commenter)
    finally:
        owner_ctx.close()

    # Finally the commenter deletes their own comment.
    ctx = _user_context(browser, commenter)
    try:
        page = ctx.new_page()
        file_viewer = _open_file(page, base_url, session_id)
        _comment_card(file_viewer, edited).get_by_role("button", name="Delete").click()
        expect(file_viewer).not_to_contain_text(edited)
        _wait_for(lambda: edited not in {c["body"] for c in _comments(base_url, session_id)})
    finally:
        ctx.close()
    assert [c["body"] for c in _comments(base_url, session_id)] == [_OWNER_COMMENT]


def test_reader_cannot_comment(
    browser: Browser,
    reviewed_session: tuple[str, str],
) -> None:
    """A Read grantee sees comments but gets no way to write one.

    Contrast with the commenter journey: the composer shows the read-only
    placeholder, the comments panel says so, selecting code offers no
    "Add comment", and the server refuses a comment POST.
    """
    base_url, session_id = reviewed_session
    reader = f"reader-{uuid.uuid4().hex[:6]}@ui.test"
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": reader, "level": _LEVEL_READ},
        timeout=10.0,
    ).raise_for_status()

    ctx = _user_context(browser, reader)
    try:
        page = ctx.new_page()
        file_viewer = _open_file(page, base_url, session_id, show_comments=False)

        composer = page.get_by_placeholder(_READ_ONLY_PLACEHOLDER)
        expect(composer).to_be_visible(timeout=15_000)
        expect(composer).to_be_disabled()

        file_viewer.get_by_text(_ANCHOR_WORD).first.dblclick()
        # The word is selected, the same state that surfaces the button for
        # a commenter; give the handler a moment before asserting it didn't.
        expect(file_viewer.locator(".selected-text")).not_to_have_count(0)
        page.wait_for_timeout(1_000)
        expect(page.get_by_role("button", name=_ADD_COMMENT_BUTTON)).to_have_count(0)

        file_viewer.get_by_role("button", name="Show comments").click()
        expect(file_viewer).to_contain_text("You have read-only access to this session.")
        owner_card = _comment_card(file_viewer, _OWNER_COMMENT)
        expect(owner_card).to_be_visible()
        expect(owner_card.get_by_role("button", name="Edit", exact=True)).to_have_count(0)
        expect(owner_card.get_by_role("button", name="Delete")).to_have_count(0)
    finally:
        ctx.close()

    start = _FILE_CONTENT.index(_ANCHOR_WORD)
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": "reader should not be able to post this",
            "start_index": start,
            "end_index": start + len(_ANCHOR_WORD),
            "anchor_content": _ANCHOR_WORD,
        },
        headers={"X-Forwarded-Email": reader},
        timeout=10.0,
    )
    assert resp.status_code == 403, resp.text
