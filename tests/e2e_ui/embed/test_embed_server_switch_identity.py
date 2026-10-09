"""Embedded web UI must drop the previous Server's user when the host switches Servers.

``resolveIdentity()`` (``web/src/lib/identity.ts``) cached the first ``GET /v1/me``
answer for the page, so a host that pointed the embed at another Server kept the
previous user: the new Server was never asked ``/v1/me``, admin-only chrome kept
the old admin flag, ``X-Forwarded-Email`` carried the old user (the same identity
feeds extension-storage namespaces), and a late answer from the previous Server
was adopted.

Journey (``OmnigentApp`` mounted by a host page owning router, transport and
auth, both Servers proxied behind one origin; see ``_embed_host_harness``): the
host connects the embed to Server A as alice (an admin there), then switches it
to Server B as bob (not an admin). Expected: the embed asks Server B ``/v1/me``,
uses nothing of alice's identity there, and the admin-only chrome disappears.

Gated e2e suite: needs the JS toolchain for the host-page Vite build
(``web/vite.e2e-embed-host.config.ts``); see the package ``conftest`` docstring.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.e2e_ui.embed._embed_host_harness import (
    embed_host_proxy,
    spawn_header_auth_server,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
_HARNESS_DIST = _WEB_DIR / "dist-e2e-embed-host"
_HOST_PAGE = "/e2e-embed-host/index.html"

# The host signs requests to Server A as alice (on Server A's admin roster) and
# requests to Server B as bob (no admin rights there).
ALICE = "alice@example.test"
BOB = "bob@example.test"


@dataclass
class EmbedHostStack:
    """The running harness: host page origin + the two proxied Servers."""

    base_url: str
    server_a_url: str
    server_b_url: str

    def page_url(self, *, me_delay_a_ms: int = 0) -> str:
        """The host page wired to alice-on-A / bob-on-B.

        :param me_delay_a_ms: Latency the host adds to Server A's ``/v1/me`` answers.
        """
        url = f"{self.base_url}/?userA={ALICE}&userB={BOB}"
        if me_delay_a_ms:
            url += f"&meDelayA={me_delay_a_ms}"
        return url


@pytest.fixture(scope="module")
def embed_host_dist(built_spa: None, request: pytest.FixtureRequest) -> Path:
    """Build the embed-host page (an existing build is reused with ``--ui-skip-build``).

    :param built_spa: Guarantees the JS toolchain; the host page has its own output.
    :param request: Reads ``--ui-skip-build``.
    :returns: The built harness directory.
    """
    host_page = _HARNESS_DIST / _HOST_PAGE.lstrip("/")
    if not (request.config.getoption("--ui-skip-build") and host_page.exists()):
        # The workspace vite binary directly: `pnpm exec` re-verifies deps
        # (a full re-install on CI boxes) before every exec.
        subprocess.run(
            [
                str(_WEB_DIR / "node_modules" / ".bin" / "vite"),
                "build",
                "--config",
                "vite.e2e-embed-host.config.ts",
            ],
            cwd=_WEB_DIR,
            check=True,
        )
    assert host_page.exists(), f"embed-host build produced no {host_page}"
    return _HARNESS_DIST


@pytest.fixture(scope="module")
def embed_host(
    embed_host_dist: Path,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[EmbedHostStack]:
    """Two multi-user header-auth Servers behind one embed-host origin.

    Server A's admin roster is exactly alice; Server B has no admins.
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_embed_switch")
    with (
        spawn_header_auth_server(
            mock_llm_server_url, server_tmp, name="a", admins=[ALICE]
        ) as server_a_url,
        spawn_header_auth_server(
            mock_llm_server_url, server_tmp, name="b", admins=[]
        ) as server_b_url,
        embed_host_proxy(
            embed_host_dist, _HOST_PAGE, {"a": server_a_url, "b": server_b_url}
        ) as base_url,
    ):
        yield EmbedHostStack(
            base_url=base_url, server_a_url=server_a_url, server_b_url=server_b_url
        )


def _open_host_page(browser: Browser, url: str) -> tuple[BrowserContext, Page]:
    """Open the host page in a fresh context, recording it when requested.

    The conftest recorder only patches the async API, so this sync driver passes
    ``record_video_dir`` itself.
    """
    kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs["record_video_dir"] = record_dir
        kwargs["record_video_size"] = {"width": 1280, "height": 800}
    context = browser.new_context(**kwargs)
    page = context.new_page()
    page.goto(url)
    return context, page


def _switch_to_server_b(page: Page) -> None:
    """Have the host switch the embed to Server B (new config + remount)."""
    page.get_by_test_id("host-connect-b").click()
    expect(page.get_by_test_id("host-current-server")).to_contain_text("Server B")


def test_admin_chrome_follows_new_server_user_after_switch(
    browser: Browser, embed_host: EmbedHostStack
) -> None:
    """Admin-only settings chrome must reflect the new Server's user.

    Alice's Members / Policies group shows on Server A and must disappear once
    the host switches the embed to Server B, where bob is not an admin.
    """
    context, page = _open_host_page(browser, embed_host.page_url())
    try:
        page.get_by_test_id("settings-button").click()
        members = page.get_by_test_id("settings-nav-members")
        policies = page.get_by_test_id("settings-nav-policies")
        # Precondition (guards a vacuous pass): alice's admin chrome is up.
        expect(members).to_be_visible(timeout=30_000)
        expect(policies).to_be_visible()

        _switch_to_server_b(page)

        # The remounted embed is still on /settings; bob has no admin group.
        expect(members).to_be_hidden(timeout=10_000)
        expect(policies).to_be_hidden()
    finally:
        context.close()


def test_new_server_is_asked_for_identity_and_previous_user_is_not_sent(
    browser: Browser, embed_host: EmbedHostStack
) -> None:
    """After the switch, identity is re-resolved on Server B and alice is not reused.

    Via the host page's hooks: no request to Server B carries alice on
    ``X-Forwarded-Email``, Server B is asked ``GET /v1/me``, and the resolved
    identity (what extension storage namespaces are built from) is (bob, server-b).
    """
    context, page = _open_host_page(browser, embed_host.page_url())
    try:
        page.wait_for_function(
            "() => window.omnigentE2EHost.sent.a.some((r) => r.path.startsWith('/v1/me'))"
        )
        probe = page.evaluate("() => window.omnigentE2EHost.identity()")
        assert probe["userId"] == ALICE, f"precondition: expected alice on Server A, got {probe}"
        assert probe["isAdmin"] is True, f"precondition: alice must be admin on Server A: {probe}"

        _switch_to_server_b(page)

        page.wait_for_function("() => window.omnigentE2EHost.sent.b.length > 0")
        stale_stamped = page.evaluate(
            "(email) => window.omnigentE2EHost.sent.b"
            + ".filter((r) => r.embedForwardedEmail === email).map((r) => r.path)",
            ALICE,
        )
        assert stale_stamped == [], (
            "requests to Server B carried the previous Server's user "
            f"({ALICE}) on X-Forwarded-Email: {stale_stamped}"
        )
        page.wait_for_function(
            "() => window.omnigentE2EHost.sent.b.some((r) => r.path.startsWith('/v1/me'))",
            timeout=10_000,
        )
        probe = page.evaluate("() => window.omnigentE2EHost.identity()")
        assert probe["serverIdentity"] == "server-b", probe
        assert probe["userId"] == BOB, (
            "after the switch the embed still resolves the previous Server's "
            f"user: {probe} (expected userId={BOB!r}) — extension storage "
            "would namespace Server B data under alice"
        )
    finally:
        context.close()


def test_late_previous_server_identity_answer_is_not_adopted(
    browser: Browser, embed_host: EmbedHostStack
) -> None:
    """A ``/v1/me`` answer from Server A landing after the switch is ignored.

    Server A answers ``/v1/me`` after 4s and the host switches mid-flight; once
    the late answer has landed, Settings on Server B shows no admin group for bob.
    """
    context, page = _open_host_page(browser, embed_host.page_url(me_delay_a_ms=4_000))
    try:
        # The host records the request before delaying its answer.
        page.wait_for_function(
            "() => window.omnigentE2EHost.sent.a.some((r) => r.path.startsWith('/v1/me'))"
        )
        _switch_to_server_b(page)
        # Outlast Server A's late answer before reading identity-gated chrome.
        page.wait_for_timeout(6_000)

        page.get_by_test_id("settings-button").click()
        expect(page.get_by_test_id("settings-nav-general")).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("settings-nav-members")).to_be_hidden(timeout=5_000)
        expect(page.get_by_test_id("settings-nav-policies")).to_be_hidden()

        probe = page.evaluate("() => window.omnigentE2EHost.identity()")
        assert probe["userId"] == BOB, (
            "the previous Server's late /v1/me answer was adopted as the new "
            f"Server's identity: {probe} (expected userId={BOB!r})"
        )
        assert probe["isAdmin"] is False, (
            f"the previous Server's admin flag survived the switch: {probe}"
        )
    finally:
        context.close()
