"""E2E: opencode-native surfaces its live model as a read-only pill.

opencode owns its model (the user switches it inside the opencode TUI), but it
mirrors the live model into the session's ``model_override`` — set at launch and
updated by the forwarder on every in-TUI switch. The web UI must surface *that*
in the model pill so the indicator tracks the TUI, even though opencode ships no
switchable web model list (the dropdown stays empty / display-only).
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

# Launch-resolved default the runner booted opencode with.
LAUNCH_MODEL = "openrouter/nemotron"
# The model the user switched to inside the opencode TUI; the forwarder mirrored
# it into ``model_override``. This — not the launch default — must show.
LIVE_TUI_MODEL = "openrouter/llama-3.3-70b-instruct"


def _patch_session_as_opencode_native(page: Page, session_id: str) -> None:
    """Patch the browser's session snapshot into an opencode-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch rewrites only the
    ``GET /v1/sessions/{session_id}`` response as seen by the browser, mirroring
    the AP snapshot after an opencode-native runner has mirrored its live TUI
    model into ``model_override``. opencode exposes no switchable web model
    list, so ``model_options`` stays absent.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: None.
    """

    def _handle(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return

        response = route.fetch()
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "opencode-native-ui",
        }
        payload["harness"] = "opencode"
        payload["llm_model"] = LAUNCH_MODEL
        payload["model_override"] = LIVE_TUI_MODEL
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_opencode_native_pill_shows_live_tui_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The model pill surfaces opencode's mirrored ``model_override``.

    Covers the PR's user-facing path: an opencode-native session shows its live
    model (the override the forwarder mirrors from the TUI), not the stale
    launch default, and the harness identity reads "OpenCode".

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to opencode-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_opencode_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # The pill mirrors the live TUI model (the override), NOT the launch default.
    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_contain_text(LIVE_TUI_MODEL, timeout=15_000)
    expect(trigger).not_to_contain_text(LAUNCH_MODEL)

    # opencode is identified as its own native wrapper in the status tray.
    expect(page.get_by_test_id("composer-harness")).to_contain_text("OpenCode")

    # Display-only: opencode ships no switchable web model rows. (Switching
    # stays in the opencode TUI; the web pill only reflects it.)
    expect(page.locator('[data-testid="model-picker-item"]')).to_have_count(0)
