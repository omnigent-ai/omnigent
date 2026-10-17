"""E2E (hermetic): the Codex composer permissions dropdown must offer bypass.

For a Codex session, the composer's quick permissions pill (``✋ Default ⌄``)
opens a dropdown listing only ``Default``, ``Full access``, and ``Read only``.
``Bypass approvals & sandbox`` — a valid Codex approval stance the same
screen's ``Edit → Advanced settings → Permissions`` select *does* offer — is
missing, so the two controls disagree about which modes exist and the primary
pill gives no hint the most-permissive stance is available.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, create) are faked, exactly like the
sibling tests in ``test_start_session.py``.

Red while the bug lives:

* ``test_codex_quick_dropdown_offers_bypass`` — the quick dropdown renders no
  ``Bypass approvals & sandbox`` item, so the pick/pill/create tail never runs.
* ``test_codex_quick_dropdown_matches_advanced_settings`` — the quick
  dropdown's mode set is a strict subset of the Advanced settings Approval
  select's (missing bypass), so the one-source-of-truth equality fails.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _open_entry_config,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

# The dangerous full-bypass stance rides the create call as this conversation
# label (not terminal_launch_args); the runner then launches Codex with
# `--dangerously-bypass-approvals-and-sandbox`.
_BYPASS_LABEL_KEY = "omnigent.codex_native.bypass_sandbox"
_BYPASS_OPTION_LABEL = "Bypass approvals & sandbox"
# The three preset stances the quick dropdown already offers.
_PRESET_LABELS = ("Default", "Full access", "Read only")


async def _land_on_codex_composer(page, base_url: str) -> None:
    """Open the new-chat landing screen with the stubbed Codex agent selected.

    Registers the shared route stubs' localStorage seed, navigates to the
    landing screen, and waits for the composer input. The stubbed Codex agent
    is the only one offered, so it auto-selects and the Approval pill renders
    without an explicit pick.

    :param page: The Playwright page (routes already registered).
    :param base_url: The spawned test server's base URL.
    """
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)


async def _stub_agent_scan(page) -> None:
    """Neutralize agent discovery so only the stubbed Codex agent feeds the picker."""

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


def test_codex_quick_dropdown_offers_bypass(seeded_session: tuple[str, str]) -> None:
    """The Codex composer permissions dropdown offers and arms full bypass.

    With a Codex agent selected on the new-chat landing screen, clicking the
    composer's permissions pill must list ``Bypass approvals & sandbox``
    alongside ``Default``, ``Full access``, and ``Read only``. Picking it must
    read back on the pill (analogous to "Bypass permissions" on Claude Code)
    and ride the create ``POST /v1/sessions`` as the
    ``omnigent.codex_native.bypass_sandbox: "1"`` conversation label with no
    ``--sandbox`` / ``--ask-for-approval`` preset flags — exactly what picking
    the same stance in Advanced settings already does.

    Red while the bug lives: the quick dropdown builds from the three-preset
    list only, so no bypass item renders.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_quick_dropdown_offers_bypass(base_url, session_id))


async def _drive_quick_dropdown_offers_bypass(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so the `finally` can close IT before the browser —
        # closing only the browser can drop an in-flight video recording
        # (OMNIGENT_E2E_RECORD_DIR) on the floor as a 0-byte file.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )
            await _stub_agent_scan(page)
            await _land_on_codex_composer(page, base_url)

            # Step: click the permissions pill (`✋ Default ⌄`).
            chip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(chip).to_be_visible(timeout=30_000)
            await expect(chip).to_contain_text("Default")
            await chip.click()

            menu = page.get_by_test_id("new-chat-landing-permission-menu")
            await expect(menu).to_be_visible()
            for label in _PRESET_LABELS:
                await expect(menu.get_by_role("menuitem", name=label, exact=True)).to_be_visible()

            # The bug: no "Bypass approvals & sandbox" item in the quick menu.
            bypass_item = page.get_by_test_id("new-chat-landing-permission-option-bypass")
            await expect(bypass_item).to_be_visible()
            await expect(bypass_item).to_contain_text(_BYPASS_OPTION_LABEL)

            # Picking it must read back on the pill…
            await bypass_item.click()
            await expect(menu).to_be_hidden()
            await expect(chip).to_contain_text(_BYPASS_OPTION_LABEL)

            # …and arm the real bypass on create: the canonical conversation
            # label, with no sandbox/approval preset flags riding along.
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
            labels = body.get("labels") or {}
            assert labels.get(_BYPASS_LABEL_KEY) == "1", body
            launch_args = body.get("terminal_launch_args") or []
            assert "--sandbox" not in launch_args, body
            assert "--ask-for-approval" not in launch_args, body
        finally:
            await context.close()
            await browser.close()


def test_codex_quick_dropdown_matches_advanced_settings(
    seeded_session: tuple[str, str],
) -> None:
    """The quick dropdown and Advanced settings agree on Codex approval modes.

    The composer's permissions pill and ``Edit → Advanced settings →
    Permissions`` render the same harness capability, so they must offer the
    same mode set — one source of truth. Collects the option labels from both
    controls and asserts set equality.

    Red while the bug lives: Advanced settings offers ``Bypass approvals &
    sandbox`` but the quick dropdown does not, so the sets diverge.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_quick_dropdown_matches_advanced(base_url, session_id))


async def _drive_quick_dropdown_matches_advanced(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )
            await _stub_agent_scan(page)
            await _land_on_codex_composer(page, base_url)

            # Step: open the quick permissions dropdown and collect its modes.
            chip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(chip).to_be_visible(timeout=30_000)
            await chip.click()
            menu = page.get_by_test_id("new-chat-landing-permission-menu")
            await expect(menu).to_be_visible()
            quick_labels = set(await menu.get_by_role("menuitem").all_inner_texts())
            await page.keyboard.press("Escape")
            await expect(menu).to_be_hidden()

            # Step: open Edit → Advanced settings → the Approval select, and
            # collect the modes it offers.
            await _open_entry_config(page, "ag_codex_e2e")
            approval = page.get_by_test_id("new-chat-landing-config-approval")
            await expect(approval).to_be_visible()
            await approval.click()
            options = page.get_by_role("option")
            await expect(options.first).to_be_visible()
            advanced_labels = set(await options.all_inner_texts())

            # Sanity: Advanced settings does offer the bypass stance (the
            # report's step 4) — if this ever fails the divergence has been
            # "fixed" by removing a mode Codex supports, which is not a fix.
            assert _BYPASS_OPTION_LABEL in advanced_labels, advanced_labels

            # The bug: the quick dropdown's mode set diverges from Advanced
            # settings' (missing the bypass stance).
            assert quick_labels == advanced_labels, (
                f"quick permissions dropdown offers {sorted(quick_labels)!r} but "
                f"Advanced settings → Permissions offers {sorted(advanced_labels)!r}"
            )
        finally:
            await context.close()
            await browser.close()
