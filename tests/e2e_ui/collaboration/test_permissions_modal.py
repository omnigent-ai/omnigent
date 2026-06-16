"""UI: the Share / permissions modal interactions themselves.

The sharing-journey test (``test_sharing_journey.py``) issues every grant
through the REST API and only asserts what each identity *sees*; its
docstring calls out the share-modal UI interaction as "a separate
follow-up test". This is that test: it drives the modal's own controls
(``PermissionsModal.tsx``) — the public-access switch, the copy-link
button, the add-user grant form, the per-row level select, and revoke —
and pins each one against the server's ``/permissions`` state so a
silently-broken control can't pass.

Single owner identity (the headerless ``local`` user, same as every other
e2e_ui context), so no second browser is needed: every assertion is on the
owner's own modal plus a REST read-back. No agent run — the modal only
needs a session to exist.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

# ``__public__`` is the synthetic user id the server stores for a public
# grant (mirrors ``PUBLIC_USER`` in PermissionsModal.tsx).
_PUBLIC_USER = "__public__"
_LEVEL_READ = 1
_LEVEL_EDIT = 2


def _permissions(base_url: str, session_id: str) -> dict[str, int]:
    """Read the session's grants as a ``{user_id: level}`` map (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/permissions", timeout=10.0)
    resp.raise_for_status()
    return {p["user_id"]: p["level"] for p in resp.json()}


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
    page.get_by_role("button", name="Share session").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_text("Share this session")).to_be_visible()


def test_permissions_modal_controls_drive_server_state(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Public toggle, copy-link, grant, level-change and revoke all work.

    Walks the whole modal surface in one session so each control is
    pinned against the ``/permissions`` REST state it mutates.
    """
    base_url, session_id = seeded_session
    grantee = "alice@ui.test"
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(f"{base_url}/c/{session_id}")

    _open_share_modal(page)
    dialog = page.get_by_role("dialog")

    # ── Public access switch: off → on creates a __public__ grant ────
    public_switch = dialog.get_by_role("switch")
    expect(public_switch).not_to_be_checked()
    assert _PUBLIC_USER not in _permissions(base_url, session_id)
    public_switch.click()
    expect(public_switch).to_be_checked()
    # The grant lands server-side (poll briefly: the toggle fires an async
    # mutation, so the REST read can race the optimistic UI flip).
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
