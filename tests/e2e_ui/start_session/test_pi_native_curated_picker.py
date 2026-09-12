"""E2E (hermetic) SPA guard: the pi-native pre-launch picker stays curated.

On the own-login path (no omnigent-managed Pi provider) a host whose Pi is
logged into several providers at once used to answer the pre-launch
model-options request with the union of every authed provider's full catalog
— hundreds of rows once OpenRouter is among the logins — burying the model
the user curated with Pi's ``enabledModels``. The server now scopes that
catalog to Pi's own curation (``pi_own_login_model_options()``); the durable
server-side guards live in ``tests/e2e/test_pi_native_picker_enabled_models.py``.

This is the *web-surface* companion: it drives the **real SPA** with the
model-options edge stubbed to the curated response the fixed server returns
for such a host, and asserts the Configure-Pi dialog's Model picker offers
exactly that curation — the enabled model immediately visible, none of the
multi-vendor OpenRouter flood. It also serves as the recording driver for the
after-fix footage of this journey.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly
like the sibling tests in ``test_start_session.py`` and
``test_pi_native_empty_picker.py``.
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

# Pi's own curation (settings.json enabledModels) scopes the picker to this
# one model; the fixed server returns exactly this instead of the union of
# every logged-in provider's full catalog.
_ENABLED_MODEL = "anthropic/claude-sonnet-4-5"


def _curated_pi_model_options() -> list[dict[str, str]]:
    """Build the curated catalog the fixed server returns for a scoped host.

    A multi-login host (anthropic, openai, google, openrouter) whose Pi
    settings curate ``enabledModels`` to one model now answers the pre-launch
    model-options request with just that scope, shaped like
    ``pi_own_login_model_options()`` output (qualified ``provider/model``
    ids).

    :returns: Picker options for the stubbed model-options edge.
    """
    return [
        {"id": _ENABLED_MODEL, "model": _ENABLED_MODEL, "displayName": "Claude Sonnet 4.5"},
    ]


def test_pi_native_prelaunch_picker_offers_only_the_curated_scope(
    seeded_session: tuple[str, str],
) -> None:
    """The Configure-Pi model picker lists the curated scope, not a flood.

    With the host's ``pi-native`` model-options answering the curated scope a
    multi-login host now returns, the pre-launch picker must render exactly
    those rows — the enabled model visible, no OpenRouter multi-vendor rows —
    so the dialog matches what Pi's own Ctrl+P picker cycles.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_curated_pi_picker(base_url, session_id))


async def _drive_curated_pi_picker(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # An explicit context (not browser.new_page()) so closing it finalizes
        # the recorded video reliably when OMNIGENT_E2E_RECORD_DIR is set -- the
        # e2e_ui conftest injects `record_video_dir` into new_context.
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
            # (sibling pi-native drivers do the same): the landing picker merges
            # `/v1/agents` with agents found by scanning the caller's sessions,
            # and leftover sessions on the shared e2e_ui server would otherwise
            # leak in and auto-select ahead of Pi.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            # The fixed edge: the multi-login unmanaged host answers with Pi's
            # own enabledModels curation (see the server-side guards in
            # tests/e2e/test_pi_native_picker_enabled_models.py).
            curated = _curated_pi_model_options()

            async def handle_pi_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": curated}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options",
                handle_pi_model_options,
            )

            # Seed a recent working directory so a real (non-sandbox) host
            # workspace is selected -- pi model options are only fetched for a
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

            # Open the picker popover and inspect its rows.
            await model_trigger.click()
            await page.get_by_test_id("new-chat-landing-config-model-search").wait_for(
                state="visible", timeout=10_000
            )

            # The fix, made observable: the picker offers exactly the curated
            # scope -- the enabled model is immediately visible, and none of
            # the multi-vendor OpenRouter catalog floods the list.
            enabled_row = page.locator(f'[data-model-id="{_ENABLED_MODEL}"]')
            await expect(enabled_row).to_be_visible(timeout=10_000)
            model_rows = page.locator("[data-model-id]")
            row_count = await model_rows.count()
            assert row_count == len(curated), (
                "the pre-launch pi-native picker did not honor the curated "
                f"scope: it rendered {row_count} model rows for a "
                f"{len(curated)}-row catalog."
            )
            assert await page.locator('[data-model-id^="openrouter/"]').count() == 0, (
                "the OpenRouter multi-vendor flood leaked back into the picker"
            )

            # Pick the curated model so the recording ends on the visible
            # outcome: a tidy picker and the chosen default applied.
            await enabled_row.click()
            await page.wait_for_timeout(1200)
        finally:
            # Close the context first so the recorded video is flushed to disk,
            # then tear the browser down.
            await context.close()
            await browser.close()
