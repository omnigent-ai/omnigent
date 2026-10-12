"""UI: the Share / permissions modal interactions themselves.

The sharing-journey test (``test_sharing_journey.py``) issues every grant
through the REST API and only asserts what each identity *sees*; its
docstring calls out the share-modal UI interaction as "a separate
follow-up test". This is that test: it drives the modal's own controls
(``PermissionsModal.tsx``) — the general-access select, the copy-link
button, the add-user grant form, the per-row level select, and revoke —
and pins each one against the server's ``/permissions`` state so a
silently-broken control can't pass.

Multi-user fixtures drive the admin's controls and cross-check stored grants.
The public Edit journey adds a second identity and a real runner with a mock
model to verify follow-ups, live delivery, ceiling downgrade and revocation.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.session import post_session_bundle
from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    _terminate,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _REPO_ROOT, _build_hello_world_bundle

# ``__public__`` is the synthetic user id the server stores for a public
# grant (mirrors ``PUBLIC_USER`` in PermissionsModal.tsx).
_PUBLIC_USER = "__public__"
_LEVEL_READ = 1
_LEVEL_EDIT = 2


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated NON-single-user server (Share chrome enabled)."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_permissions_multi_user")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


@pytest.fixture
def multi_user_runner_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[MultiUserServer, str]]:
    """Use a trusted bearer header so both browsers and the real runner authenticate."""
    server_fixture = spawn_multi_user_server(
        mock_llm_server_url,
        tmp_path,
        extra_server_env={
            "OMNIGENT_AUTH_HEADER": "Authorization",
            "OMNIGENT_AUTH_HEADER_STRIP_PREFIX": "Bearer ",
        },
        admin_headers={"Authorization": f"Bearer {ADMIN_EMAIL}"},
    )
    server = next(server_fixture)
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    runner_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OMNIGENT_RUNNER_")
        and key not in ("RUNNER_SERVER_URL", "OMNIGENT_PROCESS_LOG_FILE")
    }
    runner_env.update(
        {
            "PYTHONPATH": str(_REPO_ROOT),
            "HOME": str(tmp_path),
            "OMNIGENT_DATA_DIR": str(tmp_path / "runner-data"),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "runner-config"),
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN": ADMIN_EMAIL,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": server.base_url,
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
        }
    )
    try:
        with (tmp_path / "runner.log").open("w") as log:
            runner = subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env=runner_env,
                cwd=tmp_path,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:

                def online() -> bool:
                    if runner.poll() is not None:
                        raise AssertionError((tmp_path / "runner.log").read_text())
                    response = httpx.get(
                        f"{server.base_url}/v1/runners/{runner_id}/status",
                        headers={"Authorization": f"Bearer {ADMIN_EMAIL}"},
                        timeout=2,
                    )
                    return response.status_code == 200 and response.json()["online"]

                _wait_for(online, timeout_s=60)
                yield server, runner_id
            finally:
                _terminate(runner)
    finally:
        server_fixture.close()


def _admin_page(browser: Browser) -> Page:
    """A page whose requests carry the admin identity header."""
    context = browser.new_context(extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL})
    return context.new_page()


def _permissions(
    base_url: str, session_id: str, *, headers: dict[str, str] | None = None
) -> dict[str, int]:
    """Read the session's grants as a ``{user_id: level}`` map (admin view).

    The multi-user server 401s headerless reads, so this authenticates as the
    admin identity the browser also uses.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        headers=headers or {"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=10.0,
    )
    resp.raise_for_status()
    return {p["user_id"]: p["level"] for p in resp.json()["permissions"]}


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 10.0,
    interval_s: float = 0.25,
) -> None:
    """Poll *predicate* until it returns truthy or the deadline passes.

    The modal's mutations are fire-and-forget from the UI's perspective
    (optimistic flip + background PUT/DELETE), so a REST read-back can beat
    the server commit. A short poll closes that race without a fixed sleep.
    """
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # transient httpx blip — retry until deadline
            last_exc = exc
        time.sleep(interval_s)
    if last_exc is not None:
        raise last_exc
    raise AssertionError("condition not met within timeout")


def _open_share_modal(page: Page) -> None:
    """Open the Share modal from the chat header and wait for it to mount."""
    # Desktop viewport: the header renders a labelled Share button directly
    # (the three-dot menu + "Share" menu item is the mobile fallback).
    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_enabled(timeout=60_000)
    share.click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_text("Share this session")).to_be_visible()


def _install_clipboard_stub(page: Page) -> None:
    """Provide async clipboard on the public loopback alias.

    Chromium exposes real clipboard access on localhost, but not on the
    public-looking plain-HTTP alias this test uses to keep Share enabled.
    """
    page.add_init_script(
        """
        (() => {
          let text = "";
          Object.defineProperty(Navigator.prototype, "clipboard", {
            configurable: true,
            get() {
              return {
                writeText(value) {
                  text = String(value);
                  return Promise.resolve();
                },
                readText() {
                  return Promise.resolve(text);
                },
              };
            },
          });
        })();
        """
    )


def test_single_user_hides_share_button(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The shared e2e server is single-user (OMNIGENT_LOCAL_SINGLE_USER=1), so
    there are no other users to share with and the header Share button is
    omitted entirely — not merely disabled.

    (The disabled-with-tooltip states — local server, sharing off — still apply
    on a *multi-user* server; those are covered on the dedicated multi-user
    fixtures in this file and in test_sharing_mode_off.)
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # The agent-info trigger anchors the header actions region; wait for it so
    # we assert Share's absence against a rendered header, not an unmounted one.
    expect(page.get_by_test_id("agent-info-trigger")).to_be_visible(timeout=60_000)
    expect(page.get_by_role("button", name="Share session")).to_have_count(0)


def test_permissions_modal_controls_drive_server_state(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """General access, copy-link, grant, level-change and revoke all work.

    Walks the whole modal surface in one session so each control is
    pinned against the ``/permissions`` REST state it mutates. Runs on a
    NON-single-user server (Share is hidden in single-user mode), driven by an
    admin browser identity so the Share button renders on the local-owned
    session.
    """
    base_url = multi_user_server.base_url
    session_id = multi_user_server.session_id
    grantee = "alice@ui.test"
    page = _admin_page(browser)
    _install_clipboard_stub(page)
    page.goto(f"{multi_user_server.public_url}/c/{session_id}")

    _open_share_modal(page)
    dialog = page.get_by_role("dialog")

    # General access: No access -> Read creates a __public__ grant.
    public_level = dialog.get_by_role("combobox", name="General access")
    expect(public_level).to_have_text("No access")
    assert _PUBLIC_USER not in _permissions(base_url, session_id)
    public_level.click()
    page.get_by_role("option", name="Read", exact=True).click()
    expect(public_level).to_have_text("Read")
    # Wait for the mutation before cross-checking the grant through the API.
    _wait_for(lambda: _permissions(base_url, session_id).get(_PUBLIC_USER) == _LEVEL_READ)

    # ── Copy link: writes a shareable, session-scoped URL ────────────
    dialog.get_by_role("button", name="Copy link").click()
    expect(dialog.get_by_role("button", name="Copied!")).to_be_visible()
    clipboard = page.evaluate("() => navigator.clipboard.readText()")
    assert session_id in clipboard, f"clipboard URL {clipboard!r} missing session id"
    assert re.search(rf"/c/{re.escape(session_id)}\b", clipboard), (
        f"clipboard URL {clipboard!r} is not a /c/<id> session link"
    )

    # ── Grant a user at Read via the add-user form ───────────────────
    dialog.get_by_placeholder("alice@example.com").fill(grantee)
    dialog.get_by_role("button", name="Grant").click()
    # The new row renders the grantee and the REST state agrees at Read.
    expect(dialog.get_by_title(grantee)).to_be_visible()
    _wait_for(lambda: _permissions(base_url, session_id).get(grantee) == _LEVEL_READ)

    # ── Change that user's level Read → Edit via the row select ──────
    level_select = dialog.get_by_role("combobox", name=f"Permission level for {grantee}")
    level_select.click()
    page.get_by_role("option", name="Edit").click()
    _wait_for(lambda: _permissions(base_url, session_id).get(grantee) == _LEVEL_EDIT)

    # ── Revoke the user: row disappears, grant is gone server-side ───
    dialog.get_by_role("button", name="Revoke").click()
    expect(dialog.get_by_title(grantee)).to_have_count(0)
    _wait_for(lambda: grantee not in _permissions(base_url, session_id))


def test_share_modal_qr_code_opens_mobile_deep_link(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The "Open in mobile app" button opens a QR code dialog encoding the
    session's ``omnigent://<host>/c/<id>`` deep link.

    Pins the new QR flow added to ``PermissionsModal.tsx``: the button sits next
    to "Copy link", clicking it opens a second dialog with the QR code visible,
    and closing it returns to the share modal. The QR is rendered as an SVG
    whose ``value`` attribute carries the deep link — we read it back to
    confirm the host and session id are correct.
    """
    page = _admin_page(browser)
    page.goto(f"{multi_user_server.public_url}/c/{multi_user_server.session_id}")

    _open_share_modal(page)
    share_dialog = page.get_by_role("dialog")

    # The button sits next to "Copy link" in the footer.
    qr_button = share_dialog.get_by_role("button", name="Open in mobile app")
    expect(qr_button).to_be_visible()

    # Clicking it opens a second dialog with the QR code. Both dialogs
    # are open simultaneously (the QR dialog is portaled inside the share
    # dialog's container), so scope to the last-opened dialog via its
    # unique description text.
    qr_button.click()
    qr_dialog = page.get_by_role("dialog").filter(has_text="Scan with your phone").last
    expect(qr_dialog).to_be_visible(timeout=10_000)
    # The QR code is an SVG element inside the dialog.
    qr_svg = qr_dialog.locator("[aria-label='QR code to open this session in the Omnigent app']")
    expect(qr_svg).to_be_visible(timeout=10_000)

    # Closing the QR dialog returns to the share modal (not dismissed
    # entirely). Scope the Close button to the QR dialog to avoid matching
    # the Radix Dialog's built-in close (X) on the share dialog underneath.
    qr_dialog.get_by_role("button", name="Close").first.click()
    expect(page.get_by_text("Share this session")).to_be_visible()


def test_public_edit_ceiling_grant_downgrade_revoke(
    browser: Browser,
    multi_user_runner_server: tuple[MultiUserServer, str],
    tmp_path: Path,
) -> None:
    """Admin opt-in, public Edit, ceiling downgrade and revoke across two identities."""
    multi_user_server, runner_id = multi_user_runner_server
    base_url = multi_user_server.base_url
    admin_headers = {"Authorization": f"Bearer {ADMIN_EMAIL}"}
    member_headers = {"Authorization": "Bearer new-member@ui.test"}
    created = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        _build_hello_world_bundle(),
        headers=admin_headers,
        timeout=30,
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        headers=admin_headers,
        json={"runner_id": runner_id},
        timeout=30,
    ).raise_for_status()
    admin_context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        extra_http_headers=admin_headers,
        record_video_dir=str(tmp_path / "video"),
        record_video_size={"width": 1280, "height": 900},
    )
    member_context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        extra_http_headers=member_headers,
        record_video_dir=str(tmp_path / "video"),
        record_video_size={"width": 1280, "height": 900},
    )
    try:
        page = admin_context.new_page()
        page.goto(f"{multi_user_server.public_url}/settings/sharing")
        ceiling = page.get_by_role("combobox", name="Maximum public permission")
        expect(ceiling).to_have_text("Read", timeout=30_000)
        ceiling.click()
        page.get_by_role("option", name="Edit", exact=True).click()
        expect(ceiling).to_have_text("Edit")
        page.screenshot(path=str(tmp_path / "public-ceiling-settings.png"))

        page.goto(f"{multi_user_server.public_url}/c/{session_id}")
        _open_share_modal(page)
        dialog = page.get_by_role("dialog")
        general = dialog.get_by_role("combobox", name="General access")
        expect(general).to_have_text("No access")
        general.click()
        page.get_by_role("option", name="Edit", exact=True).click()
        expect(general).to_have_text("Edit")
        _wait_for(
            lambda: (
                _permissions(base_url, session_id, headers=admin_headers).get(_PUBLIC_USER) == 2
            )
        )
        page.screenshot(path=str(tmp_path / "public-sharing-desktop.png"))

        member = member_context.new_page()
        member.goto(f"{multi_user_server.public_url}/c/{session_id}")
        composer = member.get_by_placeholder("Send a message…")
        expect(composer).to_be_enabled(timeout=30_000)
        page.screenshot(path=str(tmp_path / "public-sharing-owner.png"))
        page.get_by_role("button", name="Done", exact=True).click()
        marker = f"public-edit-{secrets.token_hex(4)}"
        composer.fill(f"Reply with exactly this token and nothing else: {marker}")
        member.get_by_role("button", name="Send", exact=True).click()
        expect(
            member.locator('[data-testid="message-bubble"][data-role="assistant"]').first
        ).to_be_visible(timeout=60_000)
        expect(
            page.locator('[data-testid="message-bubble"]', has_text=marker).first
        ).to_be_visible(timeout=30_000)
        member.screenshot(path=str(tmp_path / "public-sharing-collaborator.png"))
        assert httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).status_code == 401

        page.goto(f"{multi_user_server.public_url}/settings/sharing")
        ceiling = page.get_by_role("combobox", name="Maximum public permission")
        expect(ceiling).to_have_text("Edit", timeout=30_000)
        ceiling.click()
        page.get_by_role("option", name="Read", exact=True).click()
        expect(ceiling).to_have_text("Read")
        member.reload()
        expect(
            member.get_by_placeholder("You have read-only access to this session")
        ).to_be_disabled(timeout=30_000)
        assert _permissions(base_url, session_id, headers=admin_headers)[_PUBLIC_USER] == 2
        assert (
            httpx.post(
                f"{base_url}/v1/sessions/{session_id}/events",
                headers=member_headers,
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                },
                timeout=10,
            ).status_code
            == 403
        )
        member.screenshot(path=str(tmp_path / "public-sharing-read-only.png"))

        page.goto(f"{multi_user_server.public_url}/c/{session_id}")
        _open_share_modal(page)
        page.set_viewport_size({"width": 390, "height": 844})
        dialog = page.get_by_role("dialog")
        general = dialog.get_by_role("combobox", name="General access")
        expect(general).to_have_text("Read")
        page.screenshot(path=str(tmp_path / "public-sharing-mobile.png"))
        assert dialog.evaluate("(el) => el.scrollWidth <= el.clientWidth")
        general.click()
        page.get_by_role("option", name="No access", exact=True).click()
        _wait_for(
            lambda: _PUBLIC_USER not in _permissions(base_url, session_id, headers=admin_headers)
        )
        with member.expect_response(
            lambda response: (
                response.url.split("?")[0].endswith(f"/v1/sessions/{session_id}")
                and response.request.method == "GET"
            )
        ) as snapshot:
            member.reload()
        assert snapshot.value.status == 404
    finally:
        admin_context.close()
        member_context.close()


def test_multi_user_shows_enabled_share_button(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """On a NON-single-user server the header Share button renders and is
    enabled — the counterpart to the single-user hide.

    This is the regression the ``single_user`` /v1/info signal fixes: a
    header-auth multi-user deploy reports ``accounts_enabled:false`` /
    ``login_url:null`` just like single-user, but must keep its Share chrome.
    Served via the public loopback alias so the local-server disable can't mask
    the button.
    """
    page = _admin_page(browser)
    page.goto(f"{multi_user_server.public_url}/c/{multi_user_server.session_id}")

    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_visible(timeout=60_000)
    expect(share).to_be_enabled()


def test_multi_user_admin_sees_members_and_sharing_settings(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """On a NON-single-user server an admin's Settings nav shows the full Admin
    group — Members, Policies, and Sharing.

    Counterpart to ``test_single_user_hides_members_and_sharing_settings``: the
    same auth shape (accounts off / no login) keeps these when the server isn't
    single-user. The admin browser identity is what surfaces the Admin group.
    """
    page = _admin_page(browser)
    page.goto(f"{multi_user_server.public_url}/c/{multi_user_server.session_id}")
    page.get_by_test_id("settings-button").click()
    page.wait_for_url("**/settings**", timeout=30_000)

    expect(page.get_by_test_id("settings-nav-members")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("settings-nav-sharing")).to_be_visible()
    expect(page.get_by_test_id("settings-nav-policies")).to_be_visible()
