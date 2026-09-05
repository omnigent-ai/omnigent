"""E2E (hermetic) recording driver: the pi-native pre-launch picker is empty.

Bug (facet 1, surface ``web``): on a host where Omnigent manages **no** Pi
provider (``resolve_pi_native_provider()`` returns ``None``), the host answers
the pre-launch model-options request with an **empty** catalog even though Pi
itself is logged in with usable models. The Configure-Pi dialog's Model picker
therefore offers only the synthetic **"Default"** row and no real Pi model — the
user cannot pin a model before launch.

This is the *web-surface* companion to the durable server-side guard in
``tests/e2e/test_pi_native_unmanaged_model.py`` (which fails on the real
host daemon's empty ``model-options`` payload). The bug is entirely server-side,
so a hermetic UI test that *stubs* the model-options edge cannot itself go
red→green on a server fix — its job here is to drive the **real SPA** so the
empty picker can be filmed as before-fix footage (``recordings``). It faults
the model-options edge to ``{"models": []}`` exactly as the live unmanaged host
does, then asserts the picker surfaces only "Default".

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly like
the sibling tests in ``test_start_session.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _open_entry_config,
    _pi_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_pi_native_prelaunch_picker_empty_without_managed_provider(
    seeded_session: tuple[str, str],
) -> None:
    """Facet 1: the Configure-Pi model picker offers ONLY "Default".

    With the host's ``pi-native`` model-options faulted to the empty catalog an
    unmanaged host really returns, the pre-launch picker must show the synthetic
    "Default" row and NOT a single real Pi model (``[data-model-id]`` count 0) —
    the observable failure the user hits when Omnigent manages no Pi provider.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_empty_pi_picker(base_url, session_id))


async def _drive_empty_pi_picker(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # An explicit context (not browser.new_page()) so that closing it
        # finalizes the recorded video reliably when OMNIGENT_E2E_RECORD_DIR is
        # set — the e2e_ui conftest injects `record_video_dir` into new_context.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_pi_native_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed built-in Pi shows
            # (sibling pi-native driver does the same): the landing picker
            # merges `/v1/agents` with agents found by scanning the caller's
            # sessions, and leftover sessions on the shared e2e_ui server would
            # otherwise leak in and auto-select ahead of Pi.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            # The bug injection: the unmanaged host returns an EMPTY pi-native
            # catalog. This is the payload the real host daemon produces when
            # `resolve_pi_native_provider()` is None (see the server-side guard
            # in tests/e2e/test_pi_native_unmanaged_model.py).
            async def handle_pi_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options",
                handle_pi_model_options,
            )

            # Seed a recent working directory so a real (non-sandbox) host
            # workspace is selected — pi model options are only fetched for a
            # real host (`useHostModelOptions(hostId, "pi-native", !sandbox)`).
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the Configure-Pi dialog (Pi auto-selects as the sole agent).
            await _open_entry_config(page, "ag_pi_e2e")
            model_trigger = page.get_by_test_id("new-chat-landing-config-model")
            await expect(model_trigger).to_be_visible()
            # The untouched picker reads the synthetic "Default".
            await expect(model_trigger).to_contain_text("Default")

            # Open the picker popover and inspect its rows.
            await model_trigger.click()
            await page.get_by_test_id("new-chat-landing-config-model-search").wait_for(
                state="visible", timeout=10_000
            )

            # The bug, made observable: the ONLY thing the picker offers is the
            # synthetic "Default" row — there is not one real Pi model to pin,
            # even though Pi itself is logged in with usable models. Every real
            # model row carries `data-model-id`; an empty catalog renders none.
            await expect(page.get_by_role("option", name="Default", exact=True)).to_be_visible()
            await expect(page.locator("[data-model-id]")).to_have_count(0)
        finally:
            # Close the context first so the recorded video is flushed to disk,
            # then tear the browser down.
            await context.close()
            await browser.close()
