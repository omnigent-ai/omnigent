"""Visual-regression snapshot of the default empty view ("/").

A single committed baseline of the whole app shell at ``/`` -- the open left
sidebar plus the ``NewChatLandingScreen`` ("What should we do?") hero and
composer. The gate lives in ``.github/workflows/ui-snapshot.yml``, which renders
inside a *digest-pinned Playwright image* so the committed baseline and the PR
comparison come from the exact same renderer (screenshots differ across
rendering environments; no diff threshold reconciles two engines, so CI is the
single source of truth). See ``tests/e2e_ui/visual/README.md`` for how to update
the baseline (label the PR, or regenerate locally with Docker).

The test is marked ``@pytest.mark.visual`` so the main e2e_ui suite (which runs
on the unpinned ``ubuntu-latest``) excludes it via ``-m 'not visual'``; only the
dedicated pinned gate runs it.

Determinism strategy -- mock every HTTP call the landing makes:

``live_server`` still serves the built SPA bundle (and the WebSocket session
feed, which on a fresh server is empty), but the rendered *data* is volatile and
async: the agent catalog, host list, and session list arrive after mount, so a
naive capture races them (the picker flips from "No agents" to a real agent, the
sidebar from "Loading..." to "No active sessions"). Rather than wait-and-mask
each one, we ``page.route``-stub the landing's HTTP endpoints with fixed
fixtures (same pattern as ``tests/e2e_ui/start_session``). That makes the view a
pure function of the committed bundle + these stubs, so the snapshot needs no
masks: the host/workspace/agent chips render fixed, known values. ``/v1/info`` /
``/v1/me`` are left to the (deterministic) real server.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

# Host the composer auto-selects (the tunneled e2e runner registers none).
_HOST_ID = "host_e2e"
# Bare session list/scan endpoint: ``/v1/sessions`` with an optional query, but
# NOT ``/v1/sessions/{id}/...`` (the per-session agent enrich) nor the
# ``/v1/sessions/updates`` WebSocket. Stubbed empty so the sidebar reads "No
# active sessions" and no custom agents leak into the picker.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")

_AGENTS_BODY = {
    "data": [
        {
            "id": "ag_claude_e2e",
            "name": "claude-native-ui",
            "display_name": "Claude Code",
            "description": "Anthropic's coding agent",
            "harness": None,
            "skills": [],
        }
    ]
}
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}
_EMPTY_LIST_BODY = {"object": "list", "data": [], "has_more": False}


def _fulfill_json(route: Route, payload: object) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


@pytest.mark.visual
def test_empty_landing_matches_baseline(
    page: Page,
    live_server: str,
    assert_snapshot,
) -> None:
    """The default empty "/" view renders pixel-identical to the committed baseline.

    :param page: pytest-playwright page (fresh context per test).
    :param live_server: Base URL of the spawned ``omnigent server`` serving the
        built SPA. The landing's data calls are stubbed below, so no real agent
        catalog / host / session state (or LLM credentials) is involved.
    :param assert_snapshot: ``pytest-playwright-visual-snapshot`` fixture; writes
        the baseline under ``--update-snapshots`` and otherwise compares against
        it, failing (and emitting actual/expected/diff PNGs) on any mismatch.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    # Pin the resolved color scheme so the whole palette is deterministic
    # regardless of the runner's prefers-color-scheme default.
    page.emulate_media(color_scheme="light")

    # Stub the landing's data endpoints so the view is fully deterministic.
    page.route("**/v1/agents", lambda r: _fulfill_json(r, _AGENTS_BODY))
    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route(_FILESYSTEM_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_SESSIONS_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))

    # Seed a recent workspace for the stubbed host so the working-directory chip
    # auto-fills to a fixed value ("repo") without hitting the file browser. Set
    # before the SPA boots so the composer reads it on mount.
    page.add_init_script(
        f'window.localStorage.setItem("omnigent:recent-workspaces",'
        f' JSON.stringify({{"{_HOST_ID}": ["/work/repo"]}}));'
    )

    page.goto(f"{live_server}/")

    landing = page.get_by_test_id("new-chat-landing")
    # Generous timeout: the SPA runs a short boot probe before the landing paints.
    expect(landing).to_be_visible(timeout=30_000)
    # Wait for the async-populated regions to settle into their loaded state: the
    # agent picker (catalog resolved) and the sidebar session list.
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_be_visible(timeout=30_000)
    expect(page.get_by_text("No active sessions")).to_be_visible(timeout=30_000)

    # Settle web fonts so glyph metrics don't shift mid-capture. The expression
    # must *return* the Promise so Playwright's sync API awaits it; an arrow
    # function that calls .then() returns undefined and never waits.
    page.evaluate("document.fonts.ready")

    # Pin the focused-composer border state and kill the blinking caret, both of
    # which are otherwise time-dependent.
    page.add_style_tag(content="* { caret-color: transparent !important; }")
    page.get_by_test_id("new-chat-landing-input").focus()

    # Full viewport: the open sidebar + the hero + the composer.
    assert_snapshot(page)
