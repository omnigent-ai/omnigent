"""Sandbox model previews use the configured provider before a host exists."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _close_entry_models,
    _codex_native_agents_body,
    _managed_info_body,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

_MODELS = [
    {"id": "gateway/primary", "displayName": "Gateway Primary", "isDefault": True},
    {"id": "gateway/fast", "displayName": "Gateway Fast"},
]


@pytest.mark.parametrize("unavailable", [False, True], ids=["astra-max", "default-fallback"])
def test_optional_gateway_preview_without_a_host(
    seeded_session: tuple[str, str], tmp_path: Path, unavailable: bool
) -> None:
    _run_in_fresh_loop(_drive_gateway(*seeded_session, evidence=tmp_path, unavailable=unavailable))


async def _drive_gateway(
    base_url: str, session_id: str, *, evidence: Path, unavailable: bool
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        creates: list[dict] = []
        requests: list[str] = []
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=creates,
                agents_body=_codex_native_agents_body(),
            )
            info = json.loads(_managed_info_body())
            info.update(
                sandbox_provider="arclet",
                sandbox_provider_capabilities={"arclet": {"gateway_models": True}},
            )
            await page.route("**/v1/info", lambda route: route.fulfill(json=info))
            await page.route("**/v1/hosts", lambda route: route.fulfill(json={"hosts": []}))
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )

            async def models(route: Route) -> None:
                requests.append(route.request.url)
                await route.fulfill(
                    json={
                        "configured": False,
                        "status": "unavailable" if unavailable else "ready",
                        "models": []
                        if unavailable
                        else [
                            {
                                "id": "system.ai.gpt-6-astra",
                                "displayName": "Astra 6",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": value}
                                    for value in ("low", "medium", "high", "max")
                                ],
                            }
                        ],
                        "configuration_revision": None,
                        "provider_label": "AI Gateway",
                        "default_model": None,
                        **(
                            {
                                "error": (
                                    "Could not load AI Gateway models. "
                                    "You can use Harness default."
                                )
                            }
                            if unavailable
                            else {}
                        ),
                    }
                )

            await page.route("**/v1/sandbox-providers/*/harnesses/*/model-options*", models)
            await page.goto(base_url)
            await _open_entry_models(page, "ag_codex_e2e")
            await expect(page.get_by_test_id("sandbox-model-provider")).to_have_text("AI Gateway")
            assert requests and "/arclet/harnesses/codex-native/" in requests[-1]
            assert creates == []
            if unavailable:
                await expect(
                    page.get_by_text("Could not load AI Gateway models.", exact=False)
                ).to_be_visible()
                await page.get_by_test_id("new-chat-landing-agent-model-default").click()
            else:
                await page.get_by_test_id(
                    "new-chat-landing-agent-model-system.ai.gpt-6-astra"
                ).click()
                await page.get_by_test_id("new-chat-landing-agent-effort-max").click()
                await expect(
                    page.get_by_test_id("new-chat-landing-agent-effort-max")
                ).to_have_attribute("aria-checked", "true")
            await page.screenshot(path=str(evidence / "gateway-model-picker.png"))
            await _close_entry_models(page)
            await page.get_by_test_id("new-chat-landing-input").fill("Reply with READY.")
            await page.screenshot(path=str(evidence / "gateway-launch-ready.png"))
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(rf"/c/{session_id}$"))
            assert len(creates) == 1
            assert creates[0]["host_type"] == "managed"
            assert creates[0]["sandbox_provider"] == "arclet"
            assert "host_id" not in creates[0]
            assert "inference_configuration_revision" not in creates[0]
            if unavailable:
                assert "model_override" not in creates[0]
                assert "reasoning_effort" not in creates[0]
            else:
                assert creates[0]["model_override"] == "system.ai.gpt-6-astra"
                assert creates[0]["reasoning_effort"] == "max"
        finally:
            await browser.close()


def test_managed_model_preview_pins_revision(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, stale=False))


def test_stale_model_preview_keeps_draft(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, stale=True))


def test_unconnected_unity_links_to_integrations(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, stale=False, needs_connection=True))


async def _drive(
    base_url: str, session_id: str, *, stale: bool, needs_connection: bool = False
) -> None:
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
            info.update(
                sandbox_provider="agent_sandbox",
                databricks_features=needs_connection,
                sandbox_provider_capabilities={"agent_sandbox": {"inference_models": True}},
            )
            await page.route("**/v1/info", lambda route: route.fulfill(json=info))

            async def models(route: Route) -> None:
                catalog_requests.append(route.request.url)
                if needs_connection:
                    await route.fulfill(
                        json={
                            "configured": True,
                            "status": "unavailable",
                            "models": [],
                            "configuration_revision": None,
                            "provider_label": None,
                            "default_model": None,
                            "error": (
                                "Connect Databricks before using this harness's "
                                "Unity Gateway provider."
                            ),
                        }
                    )
                    return
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
            if needs_connection:
                await page.get_by_test_id("new-chat-landing-input").fill("Reply with READY.")
                await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_disabled()
                await page.get_by_test_id("sandbox-catalog-error-integrations-link").click()
                await expect(page).to_have_url(re.compile(r"/settings/integrations$"))
                assert catalog_requests
                assert creates == []
                return
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
