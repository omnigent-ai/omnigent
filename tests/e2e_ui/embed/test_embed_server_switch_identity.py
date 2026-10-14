"""An embedded host that switches Servers in place must get a fresh identity on the new Server.

Part of the gated e2e suite; see the ``tests/e2e_ui/conftest.py`` module docstring.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.embed._embed_host_harness import (
    SERVER_A_STATUS,
    SERVER_A_USER,
    SERVER_B_STATUS,
    SERVER_B_USER,
    EmbedHost,
    embed_host,
)

_TRANSPORT_WAIT_MS = 15_000
_IDENTITY_SETTLE_MS = 10_000


@pytest.fixture(scope="module")
def embed_host_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EmbedHost]:
    with embed_host(tmp_path_factory.mktemp("embed_host")) as host:
        yield host


def open_host_page(page: Page, host: EmbedHost, *, hold_me_a: bool = False) -> None:
    """Load the host page connected to Server A and wait for the embedded sidebar.

    With *hold_me_a*, Server A's ``/v1/me`` answer waits for ``release_me_a``.
    """
    query: dict[str, str] = {"server": "a"}
    if hold_me_a:
        query["holdMeA"] = "1"
    page.goto(f"{host.url}/?{urlencode(query)}")
    expect(page.get_by_test_id("host-status")).to_have_text(SERVER_A_STATUS)
    expect(page.get_by_test_id("settings-button")).to_be_visible()


def wait_for_request(page: Page, server: str, path_prefix: str) -> None:
    """Wait until the embed has handed the host transport a request for *server*."""
    page.wait_for_function(
        "([server, prefix]) => window.omnigentE2EHost.sent[server]"
        ".some((r) => r.path.startsWith(prefix))",
        arg=[server, path_prefix],
        timeout=_TRANSPORT_WAIT_MS,
    )


def wait_for_answer(page: Page, server: str, path_prefix: str) -> None:
    """Wait until the host transport has handed the embed a response from *server*."""
    page.wait_for_function(
        "([server, prefix]) => window.omnigentE2EHost.answered[server]"
        ".some((r) => r.path.startsWith(prefix))",
        arg=[server, path_prefix],
        timeout=_TRANSPORT_WAIT_MS,
    )


def release_me_a(page: Page) -> None:
    page.evaluate("() => window.omnigentE2EHost.releaseMeA()")


def sent_to(page: Page, server: str) -> list[dict[str, Any]]:
    return page.evaluate("(server) => window.omnigentE2EHost.sent[server]", server)


def answered_by(page: Page, server: str) -> list[dict[str, Any]]:
    return page.evaluate("(server) => window.omnigentE2EHost.answered[server]", server)


def embed_identity(page: Page) -> dict[str, Any]:
    return page.evaluate("() => window.omnigentE2EHost.identity()")


def connect_server_b(page: Page) -> None:
    page.get_by_test_id("host-connect-b").click()
    expect(page.get_by_test_id("host-status")).to_have_text(SERVER_B_STATUS)


def expect_no_admin_chrome(page: Page) -> None:
    expect(page.get_by_test_id("settings-nav-general")).to_be_visible()
    expect(page.get_by_test_id("settings-nav-policies")).to_have_count(
        0, timeout=_IDENTITY_SETTLE_MS
    )
    expect(page.get_by_test_id("settings-nav-members")).to_have_count(0)


def expect_identity_on_server_b(page: Page) -> None:
    # The embed's own probe must reach Server B before this test probes identity,
    # which would otherwise issue that request itself.
    wait_for_request(page, "b", "/v1/me")
    stamped_alice = [r["path"] for r in sent_to(page, "b") if r["embedUser"] == SERVER_A_USER]
    assert stamped_alice == [], stamped_alice
    assert embed_identity(page) == {
        "user": SERVER_B_USER,
        "isAdmin": False,
        "serverIdentity": "server-b",
    }


def test_switching_servers_rebinds_identity_and_admin_chrome(
    request: pytest.FixtureRequest, embed_host_env: EmbedHost
) -> None:
    page: Page = request.getfixturevalue("page")
    open_host_page(page, embed_host_env)
    wait_for_answer(page, "a", "/v1/me")
    page.get_by_test_id("settings-button").click()
    expect(page.get_by_test_id("settings-nav-members")).to_be_visible()
    expect(page.get_by_test_id("settings-nav-policies")).to_be_visible()

    connect_server_b(page)
    wait_for_answer(page, "b", "/v1/sessions")

    expect_identity_on_server_b(page)
    expect_no_admin_chrome(page)


def test_late_answer_from_the_previous_server_is_ignored(
    request: pytest.FixtureRequest, embed_host_env: EmbedHost
) -> None:
    page: Page = request.getfixturevalue("page")
    open_host_page(page, embed_host_env, hold_me_a=True)
    wait_for_request(page, "a", "/v1/me")
    assert not any(r["path"].startswith("/v1/me") for r in answered_by(page, "a"))

    connect_server_b(page)
    wait_for_answer(page, "b", "/v1/sessions")
    release_me_a(page)
    wait_for_answer(page, "a", "/v1/me")

    page.get_by_test_id("settings-button").click()
    expect_identity_on_server_b(page)
    expect_no_admin_chrome(page)
