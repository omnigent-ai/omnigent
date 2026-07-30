"""UI: the Share / permissions modal interactions themselves.

The sharing-journey test (``test_sharing_journey.py``) issues every grant
through the REST API and only asserts what each identity *sees*; its
docstring calls out the share-modal UI interaction as "a separate
follow-up test". This is that test: it drives the modal's own controls
(``PermissionsModal.tsx``) — the public-access switch, the copy-link
button, the add-user grant form, the per-row level select, and revoke —
and pins each one against the server's ``/permissions`` state so a
silently-broken control can't pass.

The modal-control test uses one owner identity and REST read-backs. The
approval-control test opens the same session as a shared editor and parks a
real permission hook, proving the editor can reject but cannot approve until
the owner delegates that capability. No agent run is needed.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)

# ``__public__`` is the synthetic user id the server stores for a public
# grant (mirrors ``PUBLIC_USER`` in PermissionsModal.tsx).
_PUBLIC_USER = "__public__"
_LEVEL_READ = 1
_LEVEL_EDIT = 2
_APPROVAL_CARD = '[data-testid="approval-card"]'


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated NON-single-user server (Share chrome enabled)."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_permissions_multi_user")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _admin_page(browser: Browser) -> Page:
    """A page whose requests carry the admin identity header."""
    context = browser.new_context(extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL})
    return context.new_page()


def _permission(base_url: str, session_id: str, user_id: str) -> tuple[int, bool] | None:
    """Read one session grant as ``(level, can_approve)`` (admin view).

    The multi-user server 401s headerless reads, so this authenticates as the
    admin identity the browser also uses.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=10.0,
    )
    resp.raise_for_status()
    for permission in resp.json()["permissions"]:
        if permission["user_id"] == user_id:
            return permission["level"], permission["can_approve"]
    return None


def _grant_editor(
    server: MultiUserServer,
    user_id: str,
    *,
    can_approve: bool,
) -> None:
    """Grant edit access, optionally with delegated approval authority."""
    resp = httpx.put(
        f"{server.base_url}/v1/sessions/{server.session_id}/permissions",
        json={"user_id": user_id, "level": _LEVEL_EDIT, "can_approve": can_approve},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    )
    resp.raise_for_status()


def _pending_elicitation_ids(server: MultiUserServer) -> set[str]:
    """Return pending elicitation ids from the admin-visible snapshot."""
    resp = httpx.get(
        f"{server.base_url}/v1/sessions/{server.session_id}",
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=10.0,
    )
    resp.raise_for_status()
    return {
        item["elicitation_id"]
        for item in resp.json().get("pending_elicitations") or []
        if isinstance(item.get("elicitation_id"), str)
    }


def _park_permission_hook(
    server: MultiUserServer,
    elicitation_id: str,
    sink: dict,
) -> None:
    """Park a real Claude permission hook and record its eventual verdict."""
    try:
        sink["response"] = httpx.post(
            f"{server.base_url}/v1/sessions/{server.session_id}/hooks/permission-request",
            json={
                "session_id": "claude_e2e_shared",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/tmp",
                "permission_mode": "default",
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"},
                "tool_use_id": "tool_use_shared_e2e",
                "_omnigent_elicitation_id": elicitation_id,
            },
            headers={"X-Forwarded-Email": ADMIN_EMAIL},
            timeout=120.0,
        )
    except Exception as exc:
        sink["error"] = exc


def _start_permission_hook(
    server: MultiUserServer,
    elicitation_id: str,
) -> dict:
    """Start a hook worker and wait until its approval card is parked."""
    sink: dict = {}
    worker = threading.Thread(
        target=_park_permission_hook,
        args=(server, elicitation_id, sink),
        daemon=True,
    )
    sink["worker"] = worker
    worker.start()
    _wait_for(lambda: elicitation_id in _pending_elicitation_ids(server))
    return sink


def _assert_hook_verdict(sink: dict, expected: str) -> None:
    """Wait for a parked hook and assert its Claude allow/deny verdict."""
    sink["worker"].join(timeout=30.0)
    assert not sink["worker"].is_alive(), "permission hook did not receive the UI verdict"
    assert "error" not in sink, f"permission hook failed: {sink.get('error')!r}"
    response = sink["response"]
    assert response.status_code == 200, response.text
    decision = response.json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == expected


def _resolve_for_cleanup(server: MultiUserServer, elicitation_id: str) -> None:
    """Best-effort decline so a failed assertion cannot strand a hook worker."""
    httpx.post(
        f"{server.base_url}/v1/sessions/{server.session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "decline"},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=10.0,
    )


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
    """Public toggle, copy-link, grant, approval delegation and revoke all work.

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

    # ── Public access switch: off → on creates a __public__ grant ────
    public_switch = dialog.get_by_role("switch")
    expect(public_switch).not_to_be_checked()
    assert _permission(base_url, session_id, _PUBLIC_USER) is None
    public_switch.click()
    expect(public_switch).to_be_checked()
    # The grant lands server-side (poll briefly: the toggle fires an async
    # mutation, so the REST read can race the optimistic UI flip).
    _wait_for(lambda: _permission(base_url, session_id, _PUBLIC_USER) == (_LEVEL_READ, False))

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
    _wait_for(lambda: _permission(base_url, session_id, grantee) == (_LEVEL_READ, False))

    # ── Delegate approval: Read → Edit + approve ─────────────────────
    expect(
        dialog.get_by_text("Approvers can authorize actions that use your session credentials.")
    ).to_be_visible()
    level_select = dialog.get_by_role("combobox", name=f"Permission level for {grantee}")
    level_select.click()
    page.get_by_role("option", name="Edit + approve", exact=True).click()
    expect(level_select).to_contain_text("Edit + approve")
    _wait_for(lambda: _permission(base_url, session_id, grantee) == (_LEVEL_EDIT, True))

    # ── Remove approval authority while retaining Edit access ────────
    level_select.click()
    page.get_by_role("option", name="Edit", exact=True).click()
    expect(level_select).to_contain_text("Edit")
    _wait_for(lambda: _permission(base_url, session_id, grantee) == (_LEVEL_EDIT, False))

    # ── Revoke the user: row disappears, grant is gone server-side ───
    dialog.get_by_role("button", name="Revoke").click()
    expect(dialog.get_by_title(grantee)).to_have_count(0)
    _wait_for(lambda: _permission(base_url, session_id, grantee) is None)


def test_shared_editor_can_reject_but_needs_delegation_to_approve(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """Approval controls follow the viewer's effective delegated capability."""
    server = multi_user_server
    editor = f"editor-{uuid.uuid4().hex[:8]}@ui.test"
    _grant_editor(server, editor, can_approve=False)

    context = browser.new_context(extra_http_headers={"X-Forwarded-Email": editor})
    page = context.new_page()
    elicitation_ids: list[str] = []
    sinks: list[dict] = []
    try:
        # A plain editor may stop the owner's pending action, but cannot
        # authorize it to run with the owner's session credentials.
        editor_elicitation = f"elicit_claude_{uuid.uuid4().hex}"
        elicitation_ids.append(editor_elicitation)
        sinks.append(_start_permission_hook(server, editor_elicitation))
        page.goto(f"{server.public_url}/c/{server.session_id}")

        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=30_000)
        expect(card.get_by_role("note")).to_contain_text("delegated approver")
        expect(card.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
        reject = card.get_by_role("button", name="Reject", exact=True)
        expect(reject).to_be_enabled()
        reject.click()
        _assert_hook_verdict(sinks[-1], "deny")
        _wait_for(lambda: editor_elicitation not in _pending_elicitation_ids(server))

        # Once the owner delegates approval authority, a fresh snapshot makes
        # the same editor's Approve control actionable for the next prompt.
        _grant_editor(server, editor, can_approve=True)
        delegated_elicitation = f"elicit_claude_{uuid.uuid4().hex}"
        elicitation_ids.append(delegated_elicitation)
        sinks.append(_start_permission_hook(server, delegated_elicitation))
        page.reload()

        delegated_card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(delegated_card).to_be_visible(timeout=30_000)
        expect(delegated_card.get_by_role("note")).to_have_count(0)
        approve = delegated_card.get_by_role("button", name="Approve", exact=True)
        expect(approve).to_be_enabled()
        expect(delegated_card.get_by_role("button", name="Reject", exact=True)).to_be_enabled()
        approve.click()
        _assert_hook_verdict(sinks[-1], "allow")
        _wait_for(lambda: delegated_elicitation not in _pending_elicitation_ids(server))
    finally:
        for elicitation_id in elicitation_ids:
            _resolve_for_cleanup(server, elicitation_id)
        for sink in sinks:
            sink["worker"].join(timeout=10.0)
        context.close()


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
