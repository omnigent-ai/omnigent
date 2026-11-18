"""E2E: file links, fullscreen navigation, and sharing through session grants.

Files are seeded through the real filesystem API, without an agent turn.
The sharing journey changes only the advertised single-user capability to
expose the sharing UI; session grants and recipient file reads use the real
server, runner, and permission store.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests._helpers.browser import user_page
from tests.e2e_ui.collaboration._multi_user_server import public_loopback_url
from tests.e2e_ui.conftest import _REPO_ROOT

_FILE_PATH = "shareable_note.md"
_FILE_BODY = "Unique shareable body that proves a fresh session fetched the file."
_FILE_CONTENT = f"""\
# Shareable Note

{_FILE_BODY}
"""


@pytest.fixture
def seeded_shareable_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_copy_link_is_shareable_in_a_new_browser(
    page: Page,
    browser: Browser,
    seeded_shareable_session: tuple[str, str],
) -> None:
    """Copy link → clipboard URL → opens the file in a fresh browser context."""
    base_url, session_id = seeded_shareable_session
    # Clipboard read/write needs explicit permission in headless Chromium.
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)

    # Click the copy-link toolbar action and confirm the "Copied!" feedback
    # (the button swaps to a check icon; its tooltip becomes "Copied!").
    copy_btn = file_viewer.get_by_role("button", name="Copy link to file")
    expect(copy_btn).to_be_visible()
    copy_btn.click()

    clipboard = page.evaluate("() => navigator.clipboard.readText()")
    assert re.search(rf"[?&]file={re.escape(_FILE_PATH)}", clipboard), (
        f"clipboard URL {clipboard!r} does not carry ?file={_FILE_PATH}"
    )
    assert session_id in clipboard, f"clipboard URL {clipboard!r} missing session id"

    # Open the copied link in a brand-new context — no cookies, no localStorage,
    # i.e. a different browser. The file must rehydrate purely from the URL.
    fresh_context = browser.new_context()
    try:
        fresh_page = fresh_context.new_page()
        fresh_page.goto(clipboard)
        fresh_viewer = fresh_page.locator('[data-testid="file-viewer"]:visible')
        expect(fresh_viewer).to_be_visible(timeout=20_000)
        expect(
            fresh_page.get_by_role("button", name=f"Close {_FILE_PATH}", exact=True).first
        ).to_be_visible()
        expect(fresh_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)
    finally:
        fresh_context.close()


def test_fullscreen_file_link_survives_reload_and_can_be_exited(
    page: Page,
    seeded_shareable_session: tuple[str, str],
) -> None:
    """A fullscreen file URL rehydrates, and exiting keeps the file docked."""
    base_url, session_id = seeded_shareable_session
    query = urlencode({"file": _FILE_PATH, "fullscreen": "1"})
    page.goto(f"{base_url}/c/{session_id}?{query}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    exit_fullscreen = page.get_by_role("button", name="Exit full screen", exact=True)
    expect(exit_fullscreen).to_be_visible()
    expect(exit_fullscreen).to_have_attribute("aria-pressed", "true")
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)

    page.reload()
    expect(exit_fullscreen).to_be_visible()
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)

    exit_fullscreen.click()
    expect(page.get_by_role("button", name="Full screen", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Message the agent", exact=True)).to_be_visible()
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params.get("file") == [_FILE_PATH]
    assert "fullscreen" not in params

    page.reload()
    expect(page.get_by_role("button", name="Full screen", exact=True)).to_be_visible()
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)


def test_share_file_grants_read_access_and_opens_fullscreen(
    browser: Browser,
    seeded_shareable_session: tuple[str, str],
) -> None:
    """The owner shares a file; a newly invited reader opens its copied URL."""
    base_url, session_id = seeded_shareable_session
    public_url = public_loopback_url(base_url)
    grantee = f"file-reader-{session_id}@ui.test"
    recipient_headers = {"X-Forwarded-Email": grantee}
    session_url = f"{base_url}/v1/sessions/{session_id}"
    assert httpx.get(session_url, headers=recipient_headers, timeout=10.0).status_code == 404
    assert httpx.get(session_url, timeout=10.0).json()["share_workspace_files"] is False
    environment = httpx.get(f"{session_url}/resources/environments/default", timeout=10.0)
    environment.raise_for_status()
    workspace_root = environment.json()["metadata"]["root"]
    absolute_file_path = f"{workspace_root.rstrip('/')}/{_FILE_PATH}"
    server_info = httpx.get(f"{base_url}/v1/info", timeout=10.0)
    server_info.raise_for_status()
    multi_user_info = {**server_info.json(), "single_user": False}

    with (
        user_page(browser, headers={}, viewport={"width": 1440, "height": 1000}) as owner,
        user_page(browser, headers=recipient_headers) as recipient,
    ):

        def advertise_multi_user(route: Route) -> None:
            route.fulfill(json=multi_user_info)

        owner.route("**/v1/info", advertise_multi_user)
        # Owners can open absolute workspace paths, but readers need the
        # workspace-relative form that the share dialog puts in the link.
        owner.goto(f"{public_url}/c/{session_id}?{urlencode({'file': absolute_file_path})}")
        file_viewer = owner.locator('[data-testid="file-viewer"]:visible')
        expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)
        owner.get_by_role("button", name="Full screen", exact=True).click()
        share = file_viewer.get_by_role("button", name="Share file", exact=True)
        expect(share).to_be_enabled(timeout=20_000)
        share.click()

        dialog = owner.get_by_role("dialog", name="Share this file", exact=True)
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("session")
        dialog.get_by_placeholder("alice@example.com").fill(grantee)
        with owner.expect_response(
            lambda response: (
                response.url.endswith(f"/v1/sessions/{session_id}/permissions")
                and response.request.method == "PUT"
            )
        ) as grant_response:
            dialog.get_by_role("button", name="Grant", exact=True).click()
        assert grant_response.value.status == 200
        expect(dialog.get_by_placeholder("alice@example.com")).to_have_value("")
        assert httpx.get(session_url, timeout=10.0).json()["share_workspace_files"] is True
        expect(dialog.get_by_title(grantee)).to_be_visible()

        grants = httpx.get(f"{session_url}/permissions", timeout=10.0)
        grants.raise_for_status()
        assert {p["user_id"]: p["level"] for p in grants.json()["permissions"]}[grantee] == 1

        dialog.get_by_role("button", name="Copy file link", exact=True).click()
        expect(dialog.get_by_role("button", name="Copied!", exact=True)).to_be_visible()
        # Read the real clipboard from a secure loopback origin; the public
        # HTTP alias uses the app's selected-textarea copy fallback.
        owner.context.grant_permissions(["clipboard-read"], origin=base_url)
        clipboard_page = owner.context.new_page()
        clipboard_page.goto(f"{base_url}/v1/info")
        copied_url = clipboard_page.evaluate("() => navigator.clipboard.readText()")
        clipboard_page.close()
        parsed = urlsplit(copied_url)
        assert parsed.path == f"/c/{session_id}"
        assert parse_qs(parsed.query) == {"file": [_FILE_PATH], "fullscreen": ["1"]}

        recipient.goto(copied_url)
        recipient_viewer = recipient.locator('[data-testid="file-viewer"]:visible')
        expect(recipient_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=20_000)
        recipient.get_by_role("button", name="Exit full screen", exact=True).click()
        readonly = recipient.get_by_placeholder("You have read-only access to this session")
        expect(readonly).to_be_visible()
        expect(readonly).to_be_disabled()
        expect(recipient_viewer.get_by_text(_FILE_BODY).first).to_be_visible()
