"""E2E: shared-session viewer avatars stay out of the mobile floating header.

Two authenticated viewers open the same session: Bob at the default
desktop viewport, Alice at an iPhone-class one (390x844, touch, mobile
UA). Presence rides the session SSE stream, and the chat header renders
``data-testid="presence-avatar-<email>"`` circles for the other viewer.

Guarded behavior: below the ``md`` breakpoint (768px) the other
viewer's circle must not render in the floating header pill, where it
crowds the three-dot session menu button; the viewer surfaces inside
that three-dot dropdown instead. Desktop headers keep their circles —
asserted on Bob's page, which doubles as the sync gate proving presence
went live before the mobile assertions run.
"""

from __future__ import annotations

import os
import uuid

import httpx
from playwright.sync_api import Browser, Page, expect

# iPhone-class portrait viewport — comfortably below the Tailwind ``md``
# breakpoint (768px) so every ``max-md:`` rule is in effect.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}


def _beat(page: Page) -> None:
    """Pause briefly between journey steps — only while filming a clip."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(900)


def test_viewer_avatars_stay_out_of_mobile_floating_header(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The other viewer's circle lives in the mobile menu, not the header.

    A failure means one of:

    - The mobile floating header renders presence circles beside the
      three-dot session menu again (the header assertion), or
    - the three-dot session menu stopped surfacing the other viewer
      (the menu assertion), or
    - desktop presence circles regressed (Bob's header assertion).

    :param browser: Playwright session-scoped browser; two authenticated
        contexts stand in for two users sharing the session URL.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session, created by the fixture.
    """
    base_url, session_id = seeded_session
    run_tag = uuid.uuid4().hex[:8]
    alice_email = f"alice-mobile-{run_tag}@example.com"
    bob_email = f"bob-desktop-{run_tag}@example.com"
    for grantee in (alice_email, bob_email):
        # Grant as the "local" owner (no header) — level 2 = edit.
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/permissions",
            json={"user_id": grantee, "level": 2},
            timeout=10.0,
        ).raise_for_status()

    alice_ctx = browser.new_context(
        extra_http_headers={"X-Forwarded-Email": alice_email},
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )
    bob_ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": bob_email})
    try:
        alice = alice_ctx.new_page()
        bob = bob_ctx.new_page()
        bob.goto(f"{base_url}/c/{session_id}")
        alice.goto(f"{base_url}/c/{session_id}")
        expect(alice.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
        assert alice.evaluate("matchMedia('(max-width: 767.98px)').matches"), (
            "expected the mobile (max-md) layout branch to be in effect"
        )

        # Desktop headers keep their circles. Seeing Alice on Bob's page
        # also proves the joins' presence broadcasts reached subscribers.
        expect(bob.get_by_test_id(f"presence-avatar-{alice_email}")).to_be_visible(
            timeout=15_000
        )
        # The same broadcast feeds Alice's subscription; the settle keeps a
        # not-yet-rendered circle from faking the header assertion below.
        alice.wait_for_timeout(3_000)
        _beat(alice)

        menu_trigger = alice.get_by_test_id("session-actions-menu")
        expect(menu_trigger).to_be_visible(timeout=10_000)
        # REPORTED SYMPTOM: the circle crowds the floating header pill.
        expect(alice.get_by_test_id(f"presence-avatar-{bob_email}")).not_to_be_visible()

        # The viewer belongs inside the three-dot menu on mobile instead.
        _beat(alice)
        menu_trigger.tap()
        menu = alice.get_by_role("menu")
        expect(menu).to_be_visible(timeout=5_000)
        _beat(alice)
        expect(menu.get_by_test_id(f"presence-avatar-{bob_email}")).to_be_visible(
            timeout=15_000
        )
        _beat(alice)
    finally:
        alice_ctx.close()
        bob_ctx.close()
