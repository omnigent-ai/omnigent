"""Sandbox model previews use the configured provider before a host exists."""

from __future__ import annotations

import json
import re

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _close_entry_models,
    _managed_info_body,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

_MODELS = [
    {"id": "gateway/primary", "displayName": "Gateway Primary", "isDefault": True},
    {"id": "gateway/fast", "displayName": "Gateway Fast"},
]


def test_managed_model_preview_pins_revision(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, stale=False))


def test_stale_model_preview_keeps_draft(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, stale=True))


async def _drive(base_url: str, session_id: str, *, stale: bool) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        creates: list[dict] = []
        catalog_requests: list[str] = []
        revision = "revision-1"
        try:
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=creates
            )
            info = json.loads(_managed_info_body())
            info.update(sandbox_provider="agent_sandbox", databricks_features=False)
            await page.route("**/v1/info", lambda route: route.fulfill(json=info))

            async def models(route: Route) -> None:
                catalog_requests.append(route.request.url)
                await route.fulfill(
                    json={
                        "configured": True,
                        "status": "ready",
                        "models": _MODELS if revision == "revision-1" else _MODELS[:1],
                        "configuration_revision": revision,
                        "provider_label": "Bifrost",
                        "default_model": "gateway/primary",
                    }
                )

            await page.route("**/v1/sandbox-providers/*/harnesses/*/model-options*", models)
            if stale:

                async def reject_create(route: Route) -> None:
                    nonlocal revision
                    if route.request.method != "POST":
                        await route.fallback()
                        return
                    creates.append(route.request.post_data_json)
                    revision = "revision-2"
                    await route.fulfill(
                        status=409,
                        json={
                            "detail": {
                                "code": "inference_configuration_changed",
                                "message": (
                                    "Provider configuration changed. Review the refreshed models."
                                ),
                            }
                        },
                    )

                await page.route(re.compile(r"/v1/sessions(?:\?.*)?$"), reject_create)

            await page.goto(base_url)
            await _open_entry_models(page, "ag_claude_e2e")
            await expect(page.get_by_test_id("sandbox-model-provider")).to_have_text("Bifrost")
            await expect(
                page.get_by_role("menuitemcheckbox", name="Gateway Fast", exact=True)
            ).to_be_visible()
            assert len(catalog_requests) >= 1
            assert "agent_id=ag_claude_e2e" in catalog_requests[-1]
            assert creates == []
            await page.get_by_role("menuitemcheckbox", name="Gateway Fast", exact=True).click()
            await _close_entry_models(page)
            await page.get_by_test_id("new-chat-landing-input").fill("Reply with READY.")
            await page.get_by_test_id("new-chat-landing-submit").click()
            if stale:
                await expect(page.get_by_test_id("new-chat-landing-input")).to_have_value(
                    "Reply with READY."
                )
                await expect(
                    page.get_by_text(
                        "Provider configuration changed. Review the refreshed models."
                    ).first
                ).to_be_visible()
                await _open_entry_models(page, "ag_claude_e2e")
                await expect(
                    page.get_by_role("menuitemcheckbox", name="Gateway Fast", exact=True)
                ).to_have_count(0)
                await expect(
                    page.get_by_role("menuitemcheckbox", name="Gateway Primary", exact=True)
                ).to_have_attribute("aria-checked", "true")
                assert len(creates) == 1
            else:
                await expect(page).to_have_url(re.compile(rf"/c/{session_id}$"))
            assert creates[0]["model_override"] == "gateway/fast"
            assert creates[0]["inference_configuration_revision"] == "revision-1"
            assert creates[0]["sandbox_provider"] == "agent_sandbox"
        finally:
            await browser.close()
