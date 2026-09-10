"""E2E regression: the managed-sandbox Claude picker must not offer Fable.

On a managed deployment (``managed_sandboxes_enabled: true`` — e.g. the
Databricks-hosted "Isaac" instance) the new-chat landing defaults to the
sandbox target. The sandbox has no connected host, so the Claude Code model
picker cannot resolve a live host catalog and falls back to the static
``CLAUDE_NATIVE_MODELS`` aliases — a list that leads with **Fable**, a model
the managed deployment's gateway does not serve (its Databricks model
discovery deliberately drops ``fable`` when the workspace hasn't enabled it).

A user who picks the offered "Fable" row gets a session that silently runs
the provider default instead (the runner's launch gate drops a pick the
catalog can't serve and launches the default — Opus 4.8 on the reporting
instance — with only a runner-log warning). The picker offering the
unservable model is the head of that failure chain: Fable appears selectable
in the managed UI but is unavailable, and a session started on it silently
falls back to the provider default.

This test drives the real SPA over the spawned server, shaped into the
managed deployment (the same ``/v1/info`` stub idiom as
``test_start_session.py``): sandbox target selected by default, Claude Code
agent, gear config modal open. The model dropdown must offer its rows
WITHOUT a "Fable" entry the deployment cannot serve.

Red on the bug: the sandbox path maps ``CLAUDE_NATIVE_MODELS`` verbatim, so
"Fable" is offered and the count assertion fails. Green after a fix that
stops offering unservable static rows for the managed sandbox (whether by
dropping Fable from the fallback, gating it on the live catalog, or giving
the sandbox a server-resolved catalog).
"""

from __future__ import annotations

import json
import re

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _SESSIONS_RE,
    _agents_body,
    _managed_info_body,
    _open_entry_config,
    _run_in_fresh_loop,
)

# The stubbed Claude Code agent id from ``_agents_body`` (sole agent row, so
# it auto-selects; the explicit click keeps the flow deterministic anyway).
_CLAUDE_AGENT_ID = "ag_claude_e2e"


def test_managed_sandbox_claude_picker_offers_no_fable(
    seeded_session: tuple[str, str],
) -> None:
    """The managed sandbox's Claude model picker offers no unservable Fable row.

    End-to-end: managed deployment → new-chat landing defaults to
    the sandbox → Claude Code gear config → open the Model dropdown. The
    dropdown must open and offer its rows (the "Default" sentinel is always
    present) with NO "Fable" entry — the managed gateway doesn't serve it,
    so offering it can only produce a silently-substituted session.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_managed_sandbox_model_picker(base_url, session_id))


async def _drive_managed_sandbox_model_picker(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Use an explicit context (not bare ``new_page``): Playwright finalizes
        # the recorded video on CONTEXT close, so closing the context in the
        # ``finally`` yields a non-empty ``.webm`` even when the Fable
        # assertion below fails (the before-fix clip must survive the failure).
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # Managed capability probe: makes the sandbox the offered (and,
            # with no connected hosts, the only) target — the deployment shape
            # behind the complaint.
            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            await page.route("**/v1/info", handle_info)

            # No connected hosts: the sandbox is unambiguously the target, so
            # the Claude picker has no live host catalog to resolve.
            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"hosts": []}),
                )

            await page.route("**/v1/hosts", handle_hosts)

            # Single Claude Code agent so the picker lands on claude-native.
            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            await page.route("**/v1/agents", handle_agents)

            # Neutralize agent discovery so a leaked native agent from another
            # test can't switch the picker mid-flow.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # The create POST must never really launch a sandbox from this
            # test; return the pre-seeded session id if anything sends.
            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            await page.route(_SESSIONS_RE, handle_sessions)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Managed default with no connected hosts: the sandbox target,
            # labeled by its provider ("Databricks Sandbox" for lakebox).
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_contain_text(
                "Databricks Sandbox"
            )

            # Select the Claude Code agent and open its gear config modal.
            await _open_entry_config(page, _CLAUDE_AGENT_ID)
            await page.get_by_test_id("new-chat-landing-config-model").wait_for(
                state="visible", timeout=15_000
            )

            # Open the Model dropdown. The "Default" sentinel row is always
            # rendered, so its visibility proves the dropdown is open and
            # offering rows — the Fable assertion below can't pass vacuously.
            await page.get_by_test_id("new-chat-landing-config-model").click()
            await expect(page.get_by_role("option", name="Default")).to_be_visible()

            # THE BUG: the sandbox picker falls back to the static alias list,
            # which offers "Fable" — a model the managed deployment cannot
            # serve. A fixed build offers no Fable row here.
            fable_options = page.get_by_role("option", name="Fable", exact=True)
            await expect(fable_options).to_have_count(0)
            await expect(page.locator('[data-model-id="fable"]')).to_have_count(0)
        finally:
            await context.close()
            await browser.close()
