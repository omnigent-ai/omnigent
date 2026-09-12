"""E2E (hermetic) SPA guard: the pi-native in-session picker stays curated.

A pi-native session's composer Model picker renders the catalog the running
Pi's extension pushed (``external_model_options`` → the session snapshot's
``model_options``). The extension used to push ``registry.getAvailable()`` —
the union of every logged-in provider's full catalog, hundreds of rows once a
multi-vendor OpenRouter login is present — ignoring the scope Pi resolved
from its own ``enabledModels`` settings. It now pushes that resolved scope;
the durable server-side guards live in
``tests/e2e/test_pi_native_picker_enabled_models.py``.

This is the *web-surface* companion: it shapes the browser's view of a
seeded session into a pi-native terminal session whose snapshot carries the
curated catalog the fixed extension pushes, then drives the real SPA and
asserts the composer's Model picker offers exactly that curation — the
enabled model present, none of the OpenRouter flood. It also serves as the
recording driver for the after-fix footage of the in-session journey.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_harness_render_smoke import _patch_session_as_harness
from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller

# Pi's own curation (settings.json enabledModels) scopes the session to this
# one model; the fixed extension pushes exactly this scope instead of the
# union of every logged-in provider's full catalog.
_ENABLED_MODEL = "anthropic/claude-sonnet-4-5"

_CURATED_OPTIONS = [
    {
        "id": _ENABLED_MODEL,
        "model": _ENABLED_MODEL,
        "displayName": "Claude Sonnet 4.5",
    },
]


def test_pi_native_session_picker_offers_only_the_curated_scope(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The in-session Model picker lists the pushed scope, not a flood.

    With the session snapshot carrying the curated ``model_options`` the
    fixed extension pushes for a scoped Pi, the composer's Model picker must
    render exactly those rows — the enabled model visible, no OpenRouter
    multi-vendor rows — so the web picker matches what Pi's own Ctrl+P picker
    cycles.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to pi-native.
    """
    base_url, session_id = seeded_session
    _install_stream_controller(page, session_id)
    _patch_session_as_harness(
        page,
        session_id,
        wrapper="pi-native-ui",
        harness="pi",
        llm_model=_ENABLED_MODEL,
        model_options=_CURATED_OPTIONS,
    )

    page.goto(f"{base_url}/c/{session_id}")

    expect(page.get_by_test_id("composer-config-gear")).to_be_visible(timeout=15_000)
    page.get_by_test_id("composer-config-gear").click()
    page.get_by_test_id("composer-advanced-settings").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible(timeout=10_000)
    page.get_by_test_id("composer-config-model").click()

    # The fix, made observable: the picker offers exactly the curated scope.
    enabled_row = page.locator(f'[role="option"][data-model-id="{_ENABLED_MODEL}"]')
    expect(enabled_row).to_be_visible(timeout=10_000)
    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(len(_CURATED_OPTIONS))
    expect(page.locator('[role="option"][data-model-id^="openrouter/"]')).to_have_count(0)

    # Pick the curated model so the recording ends on the visible outcome.
    enabled_row.click()
    page.wait_for_timeout(1200)
