"""UI journey: share a session, collaborate, downgrade to read-only, revoke.

Two browser identities walk the full sharing lifecycle on one session
owned by the headerless ``local`` user (the owner must be ``local``:
the runner-ownership rule forbids binding a session to another user's
runner, and the fixture runner registers headerless). Bob is a
header-identified
collaborator. Grants are issued via the API (the share-modal UI
interaction is a separate follow-up test); the UI assertions cover
what each identity SEES at every stage.

Permission-level changes are not pushed live to an open tab, so Bob
reloads after each grant change; that reload-required behavior is by
design (the level is read from the session snapshot).

The sidebar ``WS /v1/sessions/updates`` socket may not carry the
context's extra headers in all Playwright/Chromium combos, so every
assertion here is on the chat surface (snapshot fetch + SSE), never
the sidebar.

"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
)

_COMPOSER = "Ask the agent anything…"
_READONLY_PLACEHOLDER = "You have read-only access to this session"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_BUBBLE = '[data-testid="message-bubble"]'

# Permission levels mirrored from omnigent/server/auth.py.
_LEVEL_READ = 1
_LEVEL_EDIT = 2

_AGENT_INFO_TRIGGER = '[data-testid="agent-info-trigger"]'
_OWNER_ROW = '[data-testid="agent-info-session-owner"]'


@dataclass
class _SharedFixture:
    """A ``local``-owned session plus both identities' clients.

    :param session_id: The runner-bound session id.
    :param owner: Headerless httpx client (the ``local`` owner).
    :param bob: httpx client authenticated as the collaborator.
    :param bob_email: Collaborator identity.
    """

    session_id: str
    owner: httpx.Client
    bob: httpx.Client
    bob_email: str


@pytest.fixture
def shared(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_SharedFixture]:
    """Create a ``local``-owned, runner-bound hello_world session.

    Mirrors ``seeded_session`` (headerless create + bind) plus a
    Bob client for the collaborator side. Teardown deletes as the
    owner (owner-only).

    Respawns the shared runner first if a prior test in the shard killed
    it (``test_stale_stream``) — otherwise the runner-bind ``PATCH`` below
    400s ("runner is not registered"). The strided shard split
    (``conftest.pytest_collection_modifyitems``) can place the
    runner-killing test before this one in the same shard, so this fixture
    must be order-independent like the conftest session fixtures. Any
    runner this respawns is torn down with the fixture.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    # No keep-alive pooling: these clients sit idle for minutes while
    # Playwright drives the browser, and reusing a connection the
    # spawned uvicorn already closed (~5s idle timeout) flakes with
    # RemoteProtocolError. Fresh connection per request instead.
    no_pool = httpx.Limits(max_keepalive_connections=0)
    owner = httpx.Client(base_url=live_server, timeout=30.0, limits=no_pool)
    bob = httpx.Client(
        base_url=live_server,
        headers={"X-Forwarded-Email": f"bob-{suffix}@ui.test"},
        timeout=30.0,
        limits=no_pool,
    )
    create_resp = owner.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    owner.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": str(_server_state["runner_id"])},
    ).raise_for_status()
    try:
        yield _SharedFixture(
            session_id=session_id,
            owner=owner,
            bob=bob,
            bob_email=bob.headers["X-Forwarded-Email"],
        )
    finally:
        owner.delete(f"/v1/sessions/{session_id}")
        owner.close()
        bob.close()
        # Restore the "found" state: if we respawned the runner (a prior
        # test had killed it), tear our copy down so it doesn't outlive us.
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _user_context(browser: Browser, email: str) -> BrowserContext:
    """A browser context whose fetch/XHR/SSE requests carry *email*."""
    return browser.new_context(extra_http_headers={"X-Forwarded-Email": email})


def _goto_expecting_snapshot(page: Page, base_url: str, session_id: str) -> int:
    """Navigate to the session and return the snapshot GET's status code.

    Pinning the snapshot response status makes the "no access" UI
    assertions deterministic: a 404 means the SPA cannot render a
    composer afterwards, with no settle-time race.
    """
    with page.expect_response(
        # Match on the path: the chat surface requests the slim snapshot
        # (?include_items=false&include_liveness=false), so the URL no
        # longer *ends* with the session id.
        lambda r: (
            r.url.split("?")[0].endswith(f"/v1/sessions/{session_id}")
            and r.request.method == "GET"
        )
    ) as resp_info:
        page.goto(f"{base_url}/c/{session_id}")
    return resp_info.value.status


def test_share_grant_downgrade_revoke_journey(
    browser: Browser,
    live_server: str,
    shared: _SharedFixture,
) -> None:
    sid = shared.session_id
    marker = f"bob-turn-{uuid.uuid4().hex[:8]}"
    # The owner context is headerless (the ``local`` identity), same
    # as every other e2e_ui browser context.
    owner_ctx = browser.new_context()
    bob_ctx = _user_context(browser, shared.bob_email)
    try:
        # ── Pre-grant: Bob has nothing, and is told nothing ──────
        bob_page = bob_ctx.new_page()
        status = _goto_expecting_snapshot(bob_page, live_server, sid)
        # 404 (not 403): existence must not leak to ungranted users.
        assert status == 404
        expect(bob_page.get_by_placeholder(_COMPOSER)).to_have_count(0)

        # ── Grant EDIT; Bob collaborates; owner sees it live ─────
        owner_page = owner_ctx.new_page()
        assert _goto_expecting_snapshot(owner_page, live_server, sid) == 200

        shared.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": shared.bob_email, "level": _LEVEL_EDIT},
        ).raise_for_status()
        assert _goto_expecting_snapshot(bob_page, live_server, sid) == 200
        composer = bob_page.get_by_placeholder(_COMPOSER)
        expect(composer).to_be_visible()
        composer.fill(f"Reply with exactly this token and nothing else: {marker}")
        bob_page.get_by_role("button", name="Send", exact=True).click()
        # Bob's own surfaces: his user bubble, then a real reply.
        expect(bob_page.locator(_BUBBLE, has_text=marker).first).to_be_visible(timeout=15_000)
        expect(bob_page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
        # The owner's already-open tab receives Bob's message over the
        # live stream (no reload): the collab-realtime broadcast path.
        expect(owner_page.locator(_BUBBLE, has_text=marker).first).to_be_visible(timeout=30_000)

        # ── Downgrade to READ: composer locks after reload ───────
        shared.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": shared.bob_email, "level": _LEVEL_READ},
        ).raise_for_status()
        assert _goto_expecting_snapshot(bob_page, live_server, sid) == 200
        readonly = bob_page.get_by_placeholder(_READONLY_PLACEHOLDER)
        expect(readonly).to_be_visible(timeout=15_000)
        expect(readonly).to_be_disabled()
        # The API agrees: posting a turn with READ is 403 (has SOME
        # access, so forbidden rather than 404).
        send = shared.bob.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
        assert send.status_code == 403

        # ── Revoke: Bob is back to anti-enumeration 404s ─────────
        shared.owner.delete(
            f"/v1/sessions/{sid}/permissions/{shared.bob_email}"
        ).raise_for_status()
        assert _goto_expecting_snapshot(bob_page, live_server, sid) == 404
        expect(bob_page.get_by_placeholder(_COMPOSER)).to_have_count(0)
        assert shared.bob.get(f"/v1/sessions/{sid}").status_code == 404
    finally:
        owner_ctx.close()
        bob_ctx.close()


def _open_agent_info(page: Page) -> None:
    """Open the header agent-info popover from a known-closed state.

    Escape first so we re-open from closed rather than toggling shut, mirroring
    ``agents/test_agent_info_popover.py``. The trigger is desktop-only
    (``md:inline-flex``), which the e2e viewport satisfies.
    """
    page.keyboard.press("Escape")
    trigger = page.locator(_AGENT_INFO_TRIGGER)
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()


def test_agent_info_popover_shows_session_owner(
    browser: Browser,
    live_server: str,
    shared: _SharedFixture,
) -> None:
    """The info popover's Owner row surfaces the session owner per viewer.

    Covers ``components/AgentInfo.tsx`` → ``AgentInfoContent``'s owner row
    (``GET /v1/sessions/<id>/owner`` via ``useSessionOwner``):

    * a collaborator (Bob, edit) sees the owner **without** the ``(you)`` hint;
    * the owner (headerless ``local``) sees the same row **with** ``(you)``.

    The expected owner string is read from the API, not hard-coded, so the
    assertion doesn't bake in the headerless-owner sentinel.
    """
    sid = shared.session_id
    owner_id = shared.owner.get(f"/v1/sessions/{sid}/owner").json()["owner"]
    # The row only renders for a resolvable owner; a null here would mean
    # permissions are off in this env (then the feature has nothing to show).
    assert owner_id, "expected a resolvable session owner"
    # The headerless browser context resolves to the same identity that owns the
    # session, so the owner view is the "(you)" case.
    assert shared.owner.get("/v1/me").json()["user_id"] == owner_id

    # Bob needs at least read to open the session + popover.
    shared.owner.put(
        f"/v1/sessions/{sid}/permissions",
        json={"user_id": shared.bob_email, "level": _LEVEL_EDIT},
    ).raise_for_status()

    # ── Collaborator (Bob): sees the owner, but not "(you)" ──────────
    bob_ctx = _user_context(browser, shared.bob_email)
    try:
        bob_page = bob_ctx.new_page()
        bob_page.goto(f"{live_server}/c/{sid}")
        expect(bob_page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        _open_agent_info(bob_page)
        bob_row = bob_page.locator(_OWNER_ROW)
        expect(bob_row).to_be_visible(timeout=15_000)
        expect(bob_row).to_contain_text(owner_id)
        expect(bob_row).not_to_contain_text("(you)")
    finally:
        bob_ctx.close()

    # ── Owner (headerless): same row, now with "(you)" ───────────────
    owner_ctx = browser.new_context()
    try:
        owner_page = owner_ctx.new_page()
        owner_page.goto(f"{live_server}/c/{sid}")
        expect(owner_page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        _open_agent_info(owner_page)
        owner_row = owner_page.locator(_OWNER_ROW)
        expect(owner_row).to_be_visible(timeout=15_000)
        expect(owner_row).to_contain_text("(you)")
    finally:
        owner_ctx.close()
