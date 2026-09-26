"""UI: the admin "Default visibility for new sessions" setting makes new
sessions start public.

Drives the real SPA on a dedicated multi-user server: the admin picks "All
sessions public" on Settings > Sharing, the choice survives a reload, and a
session created afterwards opens its Share dialog with Public access already
on (backed by a ``__public__`` read grant). The session seeded before the
change stays private. The data dir is isolated so the admin override file never
lands in the runner's ``~/.omnigent``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _build_hello_world_bundle

_ADMIN_HEADERS = {"X-Forwarded-Email": ADMIN_EMAIL}
_GROUP = "Default visibility for new sessions"


@pytest.fixture(scope="module")
def default_public_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated multi-user server with an isolated sharing data dir.

    The admin comes only from the admin-list file (never flagged in the
    database), so this also covers the page loading for a file-listed admin.
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_default_public")
    yield from spawn_multi_user_server(
        mock_llm_server_url,
        server_tmp,
        extra_server_env={
            "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(server_tmp / "admin-credentials"),
            "OMNIGENT_DEFAULT_PUBLIC_SESSIONS": "",
        },
    )


def _permissions(base_url: str, session_id: str) -> dict[str, int]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/permissions", headers=_ADMIN_HEADERS, timeout=10
    )
    resp.raise_for_status()
    return {p["user_id"]: p["level"] for p in resp.json()["permissions"]}


def _create_session(base_url: str) -> str:
    resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        headers=_ADMIN_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _share_dialog_public_switch(page: Page, url: str) -> Locator:
    page.goto(url)
    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_enabled(timeout=60_000)
    share.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Share this session")).to_be_visible()
    return dialog.get_by_role("switch")


def test_admin_default_public_all_makes_new_sessions_public(
    browser: Browser,
    default_public_server: MultiUserServer,
) -> None:
    server = default_public_server
    context = browser.new_context(extra_http_headers=_ADMIN_HEADERS)
    page = context.new_page()

    # ── Admin picks "All sessions public" on Settings > Sharing ──────
    page.goto(f"{server.public_url}/settings/sharing")
    group = page.get_by_role("group", name=_GROUP)
    expect(group).to_be_visible(timeout=60_000)
    expect(group.locator('input[value="off"]')).to_be_checked()
    group.get_by_text("All sessions public").click()
    expect(group.locator('input[value="all"]')).to_be_checked()

    # Persisted server-side: survives a reload.
    page.reload()
    expect(page.get_by_role("group", name=_GROUP).locator('input[value="all"]')).to_be_checked(
        timeout=30_000
    )

    # ── A session created now starts public; the older one does not ──
    new_session = _create_session(server.base_url)
    assert _permissions(server.base_url, new_session).get("__public__") == 1
    assert "__public__" not in _permissions(server.base_url, server.session_id)

    expect(
        _share_dialog_public_switch(page, f"{server.public_url}/c/{new_session}")
    ).to_be_checked()
    expect(
        _share_dialog_public_switch(page, f"{server.public_url}/c/{server.session_id}")
    ).not_to_be_checked()
    context.close()
