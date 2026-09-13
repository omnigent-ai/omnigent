"""UI: the Share control is grayed out with an explanatory tooltip when the
server's ``OMNIGENT_SHARING_MODE`` is ``off``.

Companion to ``test_permissions_modal.py::test_multi_user_shows_enabled_share_
button`` (Share enabled) and the single-user *hide* tests. The shared
session-scoped ``live_server`` runs the default policy (sharing ``on``) and is
also single-user (which hides Share outright), so this spins up a dedicated
NON-single-user server with ``OMNIGENT_SHARING_MODE=off`` and drives the real
SPA to confirm the server-side kill switch surfaces as a *disabled* Share
button — not a hidden one (single-user) and not merely a 403 on the grant
endpoint.

Served through the public-looking loopback alias so ``isCurrentServerLocal()``
is false and the *only* reason Share is disabled is the sharing-off policy —
the local-server reason would otherwise mask it. Driven by an admin browser
identity so the button renders on the local-owned session.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)

# Mirrors AppShell.tsx's shareDisabledReason for the sharing-off case.
_OFF_REASON = "Sharing has been disabled for this Omnigent server."


@pytest.fixture(scope="module")
def sharing_off_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated NON-single-user server with ``OMNIGENT_SHARING_MODE=off``.

    Multi-user (so Share isn't hidden by single-user mode) but sharing off (so
    Share is *disabled*), letting the test isolate the sharing-off disable.
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sharing_off")
    yield from spawn_multi_user_server(
        mock_llm_server_url,
        server_tmp,
        extra_server_env={"OMNIGENT_SHARING_MODE": "off"},
    )


def test_sharing_off_disables_share_button_with_tooltip(
    browser: Browser,
    sharing_off_server: MultiUserServer,
) -> None:
    """``OMNIGENT_SHARING_MODE=off`` grays out the header Share button and
    explains why — the server-side kill switch surfaced in the SPA."""
    context = browser.new_context(extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL})
    page = context.new_page()
    page.goto(f"{sharing_off_server.public_url}/c/{sharing_off_server.session_id}")

    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_visible(timeout=60_000)
    expect(share).to_be_disabled()

    page.get_by_label(f"Share session disabled: {_OFF_REASON}").hover()
    tooltip = page.locator("[data-slot=tooltip-content]", has_text=_OFF_REASON)
    expect(tooltip).to_be_visible(timeout=5_000)
    # A disabled Share control opens no dialog.
    expect(page.get_by_role("dialog")).to_have_count(0)
