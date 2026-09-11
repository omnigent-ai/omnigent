"""E2E regression guard: one canonical model label across composer surfaces.

The same selected model is labeled inconsistently across the composer surfaces.
A ``[1m]`` model whose catalog display name is ``"Fable 5.1 (1M context)"`` is
named two different ways for one selection:

* The **Models flyout** row (which *defines* the selection) reads the full
  ``"Fable 5.1 (1M context)"`` via the shared ``nativeModelLabel`` (display
  name, verbatim).
* The **new-session composer pill** and the **harness row summary** read a
  stripped ``"Fable 5.1"``, because the landing composer runs every model value
  through ``compactHarnessTriggerValue`` (``web/src/shell/NewChatDialog.tsx``),
  whose ``/ \\([^()]*context[^()]*\\)$/i`` replace deletes the ``(1M context)``
  qualifier.
* The **existing-session composer pill** reads the full
  ``"Fable 5.1 (1M context)"`` (``formatStatusModelLabel`` in
  ``web/src/pages/ChatPage.tsx`` returns the catalog display name unmodified,
  with no strip), so the *same* underlying selection is named differently on the
  new-session vs. existing-session composer.

Expected: one canonical display name per option, used
everywhere -- so a selection of the 1M variant must never render as plain
``"Fable 5.1"`` on any composer surface.

Both tests drive the real SPA in a browser; only the server edges the composer
consults (hosts, agents, model-options, and -- for the existing session -- the
session snapshot) are faked, exactly like the sibling ``start_session`` and
``chat`` model tests. They are red on the buggy build (the landing pill / row
drop ``(1M context)``) and go green once the label is canonical everywhere.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

# A claude-native host catalog that carries BOTH a plain Fable row and its
# 1M-context sibling, so the picker offers "Fable 5.1" and
# "Fable 5.1 (1M context)" as distinct rows -- exactly the reported shape (a
# separate, unchecked "Fable 5.1" sitting above the checked
# "Fable 5.1 (1M context)"). No row is the default, so the picker offers the
# "Harness default" sentinel and every real pick is explicit.
_FABLE_CATALOG: list[dict[str, Any]] = [
    {"id": "fable", "model": "system.ai.claude-fable-5", "displayName": "Fable 5.1"},
    {"id": "sonnet", "model": "system.ai.claude-sonnet-5", "displayName": "Sonnet 5"},
    {"id": "haiku", "model": "system.ai.claude-haiku-4-5", "displayName": "Haiku 4.5"},
    {
        "id": "fable[1m]",
        "model": "system.ai.claude-fable-5[1m]",
        "displayName": "Fable 5.1 (1M context)",
    },
]

_ONE_M_LABEL = "Fable 5.1 (1M context)"


async def _open_claude_landing_picker(page: Page, base_url: str) -> None:
    """Boot the landing screen with the claude-native host catalog + open its
    model picker."""

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    async def handle_model_options(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": _FABLE_CATALOG}),
        )

    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
    await page.route(
        f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
        handle_model_options,
    )
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )

    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    await _open_entry_models(page, "ag_claude_e2e")


def test_new_session_composer_strips_1m_context_from_pill_and_harness_row(
    seeded_session: tuple[str, str],
) -> None:
    """The new-session pill and harness row must keep ``(1M context)`` too.

    Reproduces the intra-picker inconsistency: inside a single open picker, the
    Models flyout names the checked selection ``"Fable 5.1 (1M context)"`` while
    the composer pill and the agent's harness-row summary drop the qualifier and
    read ``"Fable 5.1"``.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server;
        only ``base_url`` is used (the landing screen is faked).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_new_session_label_consistency(base_url, session_id))


async def _drive_new_session_label_consistency(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )
            await _open_claude_landing_picker(page, base_url)

            # Step 1 of the reported journey: select "Fable 5.1 (1M context)".
            await page.get_by_role("menuitemcheckbox", name=_ONE_M_LABEL, exact=True).click()

            # The Models flyout row DEFINES the selection: it reads the full
            # display name, and it is now the checked row.
            checked_row = page.locator(
                '[data-testid^="new-chat-landing-agent-model-"][aria-checked="true"]'
            )
            await expect(checked_row).to_contain_text(_ONE_M_LABEL)

            # The canonical label the flyout uses must also appear on the pill
            # and the harness-row summary. On the buggy build both are the
            # compact-stripped "Fable 5.1" (no "(1M context)"), so these fail.
            pill = page.get_by_test_id("new-chat-landing-agent-model-value")
            await expect(pill).to_contain_text("1M context")

            summary = page.get_by_test_id("new-chat-landing-agent-summary-ag_claude_e2e")
            await expect(summary).to_contain_text("1M context")
        finally:
            # Close the context first so a video, when recording is on, is
            # fully written before the browser (and driver) shut down.
            await page.context.close()
            await browser.close()


async def _patch_session_as_claude_native(
    page: Page,
    session_id: str,
    *,
    model_override: str,
    llm_model: str,
    model_options: list[dict[str, Any]],
) -> None:
    """Patch the browser's snapshot of ``session_id`` into a claude-native shape.

    Mirrors ``tests/e2e_ui/chat/test_claude_model_picker._patch_session_as_claude_native``
    but in the async Playwright API: rewrite only ``GET``/``PATCH
    /v1/sessions/{id}`` so the SPA renders the session as a claude-native
    wrapper bound to ``llm_model`` with ``model_override`` and the given
    catalog. Everything else is served by the real spawned server.
    """
    latest_payload: dict[str, Any] | None = None

    async def _handle(route: Route) -> None:
        nonlocal latest_payload
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}":
            await route.continue_()
            return

        if request.method == "GET":
            response = await route.fetch()
            payload = await response.json()
        elif request.method == "PATCH":
            body = json.loads(request.post_data or "{}")
            payload = dict(latest_payload or {})
            if "model_override" in body:
                payload["model_override"] = body["model_override"]
        else:
            await route.continue_()
            return

        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = llm_model
        payload["model_options"] = model_options
        payload["model_override"] = model_override
        latest_payload = dict(payload)
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    await page.route("**/v1/sessions/**", _handle)


def test_pill_label_matches_across_new_and_existing_session(
    seeded_session: tuple[str, str],
) -> None:
    """One canonical label for one selection, on BOTH composer surfaces.

    The existing-session composer pill reads the full ``"Fable 5.1 (1M
    context)"``; the new-session composer pill reads the stripped ``"Fable
    5.1"`` for the identical selection. This test asserts the two pills agree,
    so it is red on the buggy build and green once the label is canonical
    everywhere (and guards against a "fix" that instead strips the qualifier off
    both surfaces).

    :param seeded_session: ``(base_url, session_id)`` from the spawned server;
        the session is patched to a claude-native snapshot bound to the 1M model.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_cross_composer_label(base_url, session_id))


async def _drive_cross_composer_label(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            # Landing-screen edges (hosts/agents/create) for the new-session leg.
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )
            # Snapshot patch for the existing-session leg (a different URL, so it
            # coexists with the landing routes above).
            await _patch_session_as_claude_native(
                page,
                session_id,
                model_override="fable[1m]",
                llm_model="system.ai.claude-fable-5[1m]",
                model_options=_FABLE_CATALOG,
            )

            # Existing-session composer: read its settled pill label.
            await page.goto(f"{base_url}/c/{session_id}")
            existing_pill = page.get_by_test_id("composer-agent-model-value")
            await expect(existing_pill).to_contain_text(_ONE_M_LABEL, timeout=20_000)
            existing_label = (await existing_pill.text_content() or "").strip()

            # New-session composer: pick the same 1M model, read its pill label.
            await _open_claude_landing_picker(page, base_url)
            await page.get_by_role("menuitemcheckbox", name=_ONE_M_LABEL, exact=True).click()
            new_pill = page.get_by_test_id("new-chat-landing-agent-model-value")
            await expect(new_pill).to_be_visible()
            new_label = (await new_pill.text_content() or "").strip()

            # The same underlying selection must read identically on both
            # composers. On the buggy build: existing == "Fable 5.1 (1M
            # context)" but new == "Fable 5.1".
            assert new_label == existing_label, (
                "the same selected model reads a different label on the new-session "
                f"composer ({new_label!r}) than the existing-session composer "
                f"({existing_label!r}); one canonical display name must be used on "
                "every composer surface"
            )
            assert "1M context" in new_label, (
                f"the new-session composer pill dropped the (1M context) qualifier: {new_label!r}"
            )
        finally:
            # Close the context first so a video, when recording is on, is
            # fully written before the browser (and driver) shut down.
            await page.context.close()
            await browser.close()
