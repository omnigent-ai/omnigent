"""UI: the admin default-sharing switch scopes which new sessions start public.

Drives the real SPA on a dedicated multi-user server: enabling defaults to
managed hosts only, then the admin picks "All sessions". Both survive a reload. A
session created afterwards opens its Share dialog with General access set to
Read (backed by a ``__public__`` read grant). The session seeded before the
change stays private. The data dir is isolated so the admin override file never
lands in the runner's ``~/.omnigent``.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests._helpers.browser import user_page
from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    create_user_session,
    spawn_multi_user_server,
)

_ADMIN_HEADERS = {"X-Forwarded-Email": ADMIN_EMAIL}
_GROUP = "Automatically share"
_TOGGLE = "Share new sessions by default"


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


def _share_dialog_general_access(page: Page, url: str) -> Locator:
    page.goto(url)
    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_enabled(timeout=60_000)
    share.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Share this session")).to_be_visible()
    return dialog.get_by_role("combobox", name="General access")


def test_admin_default_public_all_makes_new_sessions_public(
    browser: Browser,
    default_public_server: MultiUserServer,
) -> None:
    server = default_public_server
    with user_page(browser, headers=_ADMIN_HEADERS) as page:
        page.goto(f"{server.public_url}/settings/sharing")
        toggle = page.get_by_role("switch", name=_TOGGLE)
        expect(toggle).not_to_be_checked(timeout=60_000)
        expect(page.get_by_role("group", name=_GROUP)).not_to_be_visible()
        toggle.click()
        group = page.get_by_role("group", name=_GROUP)
        expect(group.get_by_role("radio", name="Managed hosts only")).to_be_checked()

        page.reload()
        expect(group.get_by_role("radio", name="Managed hosts only")).to_be_checked(timeout=30_000)
        local_session = create_user_session(server.base_url, headers=_ADMIN_HEADERS)
        assert "__public__" not in _permissions(server.base_url, local_session)

        group.get_by_role("radio", name="All sessions", exact=True).click()
        expect(group.get_by_role("radio", name="All sessions", exact=True)).to_be_checked()

        # Persisted server-side: survives a reload.
        page.reload()
        expect(group.get_by_role("radio", name="All sessions", exact=True)).to_be_checked(
            timeout=30_000
        )

        # ── A session created now starts public; the older one does not ──
        new_session = create_user_session(server.base_url, headers=_ADMIN_HEADERS)
        assert _permissions(server.base_url, new_session).get("__public__") == 1
        assert "__public__" not in _permissions(server.base_url, server.session_id)

        toggle.click()
        expect(toggle).not_to_be_checked()
        expect(group).not_to_be_visible()
        page.reload()
        expect(toggle).not_to_be_checked(timeout=30_000)
        private_session = create_user_session(server.base_url, headers=_ADMIN_HEADERS)
        assert "__public__" not in _permissions(server.base_url, private_session)
        assert _permissions(server.base_url, new_session).get("__public__") == 1

        expect(
            _share_dialog_general_access(page, f"{server.public_url}/c/{new_session}")
        ).to_have_text("Read")
        expect(
            _share_dialog_general_access(page, f"{server.public_url}/c/{server.session_id}")
        ).to_have_text("No access")
