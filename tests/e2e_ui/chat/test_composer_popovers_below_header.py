"""Composer popovers and command output must stay below the session header.

The ChatHeader is a transparent ``z-30`` overlay pinned to ``top-0``. When the
composer's command feedback (``/help`` and other slash output) grows upward, or
a floating suggestion menu (the ``/`` slash menu, the ``@`` mention list) opens
on a short window without a viewport cap, the surface reaches into the header
band and the session title and header icons paint on top of the command text.
A bare ``/model`` additionally leaves the typed draft in the composer instead
of clearing it before showing its usage hint.

Each test drives the real journey and asserts the *correct* (post-fix) outcome,
so it fails on the buggy build and passes once the popovers are confined to a
band below the header.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller

# A long installed-skill catalog so /help output grows the card tall enough to
# reach the header band, and the slash menu has many rows on a short window.
_SKILLS = [
    {"name": f"repro-skill-{i:02d}", "description": f"Reproduction skill number {i:02d}"}
    for i in range(40)
]
_HELP_NEEDLE = "/repro-skill-00"


def _patch_session_host(page: Page, session_id: str) -> None:
    """Give the browser's session snapshot a host + workspace so skills load."""
    session_path = f"/v1/sessions/{session_id}"

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body.update(host_id="header-band-host", workspace="/workspace")
        route.fulfill(response=response, json=body)

    page.route(f"**{session_path}*", snapshot)


def _serve_skills(page: Page, session_id: str) -> None:
    page.route(
        f"**/v1/skills?session_id={session_id}",
        lambda route: route.fulfill(json={"skills": _SKILLS}),
    )


def _header_and_card(page: Page, needle: str) -> dict:
    """Header rect, composer-card rect, and the needle-holding feedback rect."""
    return page.evaluate(
        """
        (needle) => {
          const rect = (el) => {
            const b = el.getBoundingClientRect();
            return { top: b.top, bottom: b.bottom, left: b.left, right: b.right };
          };
          const header = document.querySelector('.chat-header');
          const card = document.querySelector('[data-composer-card]');
          const feedback = card
            ? [...card.querySelectorAll('.whitespace-pre-wrap')].find((el) =>
                el.textContent.includes(needle),
              )
            : null;
          return {
            header: header ? rect(header) : null,
            headerZ: header ? getComputedStyle(header).zIndex : null,
            card: card ? rect(card) : null,
            feedback: feedback ? rect(feedback) : null,
          };
        }
        """,
        needle,
    )


def _header_and_menu(page: Page, item_testid: str) -> dict:
    """Header rect and the floating suggestion-menu container rect."""
    return page.evaluate(
        """
        (itemTestId) => {
          const rect = (el) => {
            const b = el.getBoundingClientRect();
            return { top: b.top, bottom: b.bottom, left: b.left, right: b.right };
          };
          const header = document.querySelector('.chat-header');
          let menu = document.querySelector(`[data-testid="${itemTestId}"]`);
          while (
            menu &&
            !(typeof menu.className === 'string' && menu.className.includes('bottom-full'))
          ) {
            menu = menu.parentElement;
          }
          return {
            header: header ? rect(header) : null,
            menu: menu ? rect(menu) : null,
            menuZ: menu ? getComputedStyle(menu).zIndex : null,
          };
        }
        """,
        item_testid,
    )


def test_help_command_output_stays_below_header(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The /help listing must not grow the card up into the header band."""
    base_url, session_id = seeded_session
    _patch_session_host(page, session_id)
    _serve_skills(page, session_id)
    _install_stream_controller(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    # The skills catalog must be resolved so /help lists them (grows the card).
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-repro-skill-00")).to_be_visible(timeout=15_000)

    composer.fill("/help")
    page.keyboard.press("Enter")

    feedback = page.locator("[data-composer-card] .whitespace-pre-wrap").filter(
        has_text=_HELP_NEEDLE
    )
    expect(feedback).to_be_visible(timeout=15_000)

    geom = _header_and_card(page, _HELP_NEEDLE)
    assert geom["header"] is not None, "ChatHeader overlay not found"
    assert geom["card"] is not None, "composer card not found"
    assert geom["feedback"] is not None, "/help output did not render inside the composer card"
    # The header overlay paints on top of anything beneath it.
    assert geom["headerZ"] in {"30", "auto"} or int(geom["headerZ"]) >= 30

    # Correct behaviour: the /help output is confined below the header band.
    # The bug lets it grow up under the transparent header, which then paints
    # the session title/icons over the listing.
    assert geom["card"]["top"] >= geom["header"]["bottom"] - 1, (
        f"composer card top {geom['card']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} — header paints over the /help output"
    )
    assert geom["feedback"]["top"] >= geom["header"]["bottom"] - 1, (
        f"/help output top {geom['feedback']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} — header paints over the /help output"
    )


def test_slash_menu_stays_below_header_on_short_window(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The slash-command menu must be viewport-capped below the header."""
    base_url, session_id = seeded_session
    _patch_session_host(page, session_id)
    _serve_skills(page, session_id)
    _install_stream_controller(page, session_id)

    # A short window: an uncapped max-h-80 menu reaches into the header.
    page.set_viewport_size({"width": 1280, "height": 430})
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("slash-menu-item-repro-skill-00")).to_be_visible(timeout=15_000)

    geom = _header_and_menu(page, "slash-menu-item-help")
    assert geom["header"] is not None, "ChatHeader overlay not found"
    assert geom["menu"] is not None, "slash-command menu container not found"

    assert geom["menu"]["top"] >= geom["header"]["bottom"] - 1, (
        f"slash menu top {geom['menu']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} on a short window"
    )


def test_bare_model_command_clears_the_draft(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A bare /model must clear the typed draft before showing its usage hint."""
    base_url, session_id = seeded_session
    _install_stream_controller(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # Type the bare command, accept the highlighted suggestion, then submit it.
    composer.fill("/model")
    expect(page.get_by_test_id("slash-menu-item-model")).to_be_visible(timeout=15_000)
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")

    # The usage hint appears (command recognised)...
    expect(page.get_by_text(re.compile(r"Usage: /model"))).to_be_visible(timeout=15_000)
    # ...and the typed draft must be gone, matching /help and /context.
    expect(composer).to_have_value("")
