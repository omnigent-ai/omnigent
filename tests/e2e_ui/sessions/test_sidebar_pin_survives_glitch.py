"""E2E: a pinned session survives a transient backfill glitch, only a real 404 prunes it.

Pinning is client-side state persisted to ``localStorage`` under
``omnigent:pinned-conversation-ids`` (see ``sidebarNav.ts``). A pinned session
that isn't on a loaded ``GET /v1/sessions`` page is rescued by the
pinned-backfill, which fetches it by id (``usePinnedConversationBackfill`` in
``useConversations.ts``). The sidebar then normalizes the pin set and writes it
back to localStorage.

The regression these tests guard: normalization used to drop any pinned id
merely ABSENT from the loaded list. When a backfill fetch failed on a transient
client/server glitch (a 5xx, a dropped request), the off-page pin was absent
through no fault of its own — and got silently pruned, then persisted. The fix
prunes only on positive evidence of deletion: a backfill fetch that returns 404
(``fetchConversationById`` → ``null``). A fetch that throws (5xx) is NOT proof
the session is gone, so the pin is kept.

Both tests seed a real on-page pin (so a "Pinned" section renders and proves the
normalize effect ran) plus an off-page pin whose backfill is intercepted:

- 5xx (glitch) → the off-page pin must SURVIVE in localStorage.
- 404 (deleted) → the off-page pin must be PRUNED from localStorage.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

# Mirrors PINNED_CONVERSATION_IDS_STORAGE_KEY in web/src/shell/sidebarNav.ts —
# pins are client-side state, so the tests seed them where the app reads them.
_PINNED_KEY = "omnigent:pinned-conversation-ids"

# A bare-hex id for a pinned session NOT present on any loaded /v1/sessions page,
# so the sidebar's pinned-backfill fetches it by id. Bare hex avoids the legacy
# id migration rewriting the seeded value (see migratePinnedConversationIds).
_OFF_PAGE_PIN_ID = "deadbeefdeadbeefdeadbeefdeadbeef"


def _seed_pins(page: Page, ids: list[str]) -> None:
    """Seed the pinned-ids localStorage key before any app script runs."""
    page.add_init_script(
        f"window.localStorage.setItem({_PINNED_KEY!r}, {json.dumps(json.dumps(ids))})"
    )


def _stub_backfill(page: Page, pin_id: str, status: int) -> None:
    """Intercept the off-page pin's backfill fetch, forcing *status*.

    ``GET /v1/sessions/{pin_id}`` is the by-id backfill for a pinned session
    absent from the loaded list. 5xx models a transient glitch (fetch throws);
    404 models a genuinely deleted session. Only this exact path is stubbed —
    everything else (the list, the real session's rows) continues untouched.

    :param page: Playwright page before navigation.
    :param pin_id: The off-page pinned id to intercept.
    :param status: HTTP status to return (e.g. 500 or 404).
    """

    def _handler(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{pin_id}":
            route.continue_()
            return
        route.fulfill(
            status=status,
            headers={"content-type": "application/json"},
            body=json.dumps({"error": "stubbed"} if status >= 500 else {}),
        )

    page.route(f"**/v1/sessions/{pin_id}*", _handler)


def _stored_pins(page: Page) -> list[str]:
    """Read the pinned-ids array currently persisted in localStorage."""
    raw = page.evaluate(f"() => window.localStorage.getItem({_PINNED_KEY!r})")
    return json.loads(raw) if raw else []


def _pins_exclude_js(pin_id: str) -> str:
    """Browser predicate: the stored pin set no longer includes *pin_id*."""
    return (
        f"() => !JSON.parse(window.localStorage.getItem({_PINNED_KEY!r}) || '[]')"
        f".includes({pin_id!r})"
    )


def test_pin_survives_transient_backfill_glitch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An off-page pin whose backfill 5xxs stays pinned (not silently dropped).

    Seeds two pins: the real seeded session (on-page) and an off-page id whose
    backfill is forced to 500. Once the list loads and the real pin renders under
    "Pinned" (proving the normalize effect ran), the off-page id must still be in
    localStorage. Under the old drop-on-absence behaviour it would be pruned and
    the loss written back.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_pins(page, [_OFF_PAGE_PIN_ID, session_id])
    _stub_backfill(page, _OFF_PAGE_PIN_ID, status=500)

    # Wait for the off-page pin's backfill to actually fail before asserting:
    # the (buggy) prune would fire off that settled result, so the pin must have
    # had its chance to be dropped for the survival assertion to mean anything.
    with page.expect_response(
        lambda r: urlparse(r.url).path == f"/v1/sessions/{_OFF_PAGE_PIN_ID}" and r.status == 500
    ):
        page.goto(base_url)

    # The real pin rendering under "Pinned" proves the list loaded and the
    # normalize effect (which persists the pin set) has run.
    expect(
        page.locator("section")
        .filter(has=page.get_by_role("button", name="Pinned", exact=True))
        .locator(f'a[href="/c/{session_id}"]')
    ).to_be_visible(timeout=30_000)

    # The glitchy off-page pin must survive: its backfill 500'd, which can never
    # enter the confirmed-deleted set, so normalize must keep it. Under the old
    # drop-on-absence behaviour it would already be pruned from localStorage.
    stored = _stored_pins(page)
    assert _OFF_PAGE_PIN_ID in stored, f"glitchy pin was dropped: {stored}"
    assert session_id in stored


def test_pin_pruned_when_backfill_confirms_deleted(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An off-page pin whose backfill 404s is pruned — deletion cleanup still works.

    The contrast to the glitch case: a 404 IS positive evidence the session is
    gone, so its stale pin should be removed from localStorage. This proves the
    fix didn't disable pin cleanup, only narrowed it to confirmed deletions.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_pins(page, [_OFF_PAGE_PIN_ID, session_id])
    _stub_backfill(page, _OFF_PAGE_PIN_ID, status=404)

    with page.expect_response(
        lambda r: urlparse(r.url).path == f"/v1/sessions/{_OFF_PAGE_PIN_ID}" and r.status == 404
    ):
        page.goto(base_url)

    expect(
        page.locator("section")
        .filter(has=page.get_by_role("button", name="Pinned", exact=True))
        .locator(f'a[href="/c/{session_id}"]')
    ).to_be_visible(timeout=30_000)

    # The confirmed-deleted (404) pin is pruned from localStorage; the live real
    # pin is kept. Proves the fix narrowed cleanup to real deletions, not
    # disabled it.
    page.wait_for_function(_pins_exclude_js(_OFF_PAGE_PIN_ID), timeout=10_000)
    assert session_id in _stored_pins(page)
