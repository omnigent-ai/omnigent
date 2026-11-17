"""E2E: an expired sign-in shows a notice with a reload action instead of retrying silently.

The auth edge answers the live stream with its login page, and answers the identity
probe with a 303 to its login page, as it does once the sign-in has expired. The
probe does not follow redirects, so the chat must stop retrying and offer "Reload
page", which reloads the top-level page. Desktop Chromium at a phone profile stands
in for the native WebView.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, Route, expect

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 844},
    is_mobile=True,
    has_touch=True,
)

_NOTICE_HEADLINE = "Your sign-in expired"
_LOGIN_PAGE = "<!doctype html><title>Sign in</title><p>Sign in to continue.</p>"
# The edge's redirect target for an invalid session, as observed on AWS, Azure, and GCP.
_LOGIN_REDIRECT = "/login.html?next_url=%2Fomnigent%2Fv1%2Fme"
_TIMEOUT_MS = 30_000


@_PHONE
def test_expired_sign_in_shows_a_reload_notice(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The first identity read boots the app; later reads and the stream report the expiry."""
    base_url, session_id = seeded_session
    identity_reads = [0]

    def _identity(route: Route) -> None:
        identity_reads[0] += 1
        if identity_reads[0] == 1:
            route.continue_()
            return
        route.fulfill(status=303, headers={"Location": _LOGIN_REDIRECT})

    def _login_page(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=_LOGIN_PAGE)

    page.route(re.compile(r"/v1/me$"), _identity)
    page.route(re.compile(r"/login\.html"), _login_page)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}/stream"), _login_page)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("error-pill").filter(has_text=_NOTICE_HEADLINE)).to_be_visible(
        timeout=_TIMEOUT_MS
    )

    with page.expect_navigation(timeout=_TIMEOUT_MS):
        page.get_by_role("button", name="Reload page").click()
