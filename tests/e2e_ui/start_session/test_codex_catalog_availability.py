"""A Codex catalog without a default marker still offers usable models."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _close_entry_models,
    _codex_native_agents_body,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

_MODELS = [
    {
        "id": "catalog-model-a",
        "model": "catalog-model-a",
        "displayName": "Catalog Model A",
        "isDefault": False,
    },
    {
        "id": "catalog-model-b",
        "model": "catalog-model-b",
        "displayName": "Catalog Model B",
    },
]


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize(
    ("catalog_state", "pinned_model"),
    [
        pytest.param("available", None, id="available-default"),
        pytest.param("available", "catalog-model-b", id="available-explicit"),
        pytest.param("empty", None, id="empty"),
        pytest.param("failed", None, id="failed"),
    ],
)
def test_codex_prelaunch_label_reflects_catalog_availability(
    seeded_session: tuple[str, str],
    tmp_path: Path,
    width: int,
    catalog_state: str,
    pinned_model: str | None,
) -> None:
    """Default selection is distinct from an empty or failed model catalog."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(
        _drive_catalog_availability(
            base_url, session_id, tmp_path, width, catalog_state, pinned_model
        )
    )


async def _drive_catalog_availability(
    base_url: str,
    session_id: str,
    evidence_dir: Path,
    width: int,
    catalog_state: str,
    pinned_model: str | None,
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": width, "height": 900})
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )

            async def handle_catalog(route: Route) -> None:
                if catalog_state == "failed":
                    await route.fulfill(status=502, json={"detail": "Codex catalog probe failed"})
                else:
                    await route.fulfill(
                        json={"models": _MODELS if catalog_state == "available" else []}
                    )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                handle_catalog,
            )
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
            await _open_entry_models(page, "ag_codex_e2e")
            picker = page.get_by_test_id("new-chat-landing-agent-select")
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible()
            await page.screenshot(
                path=evidence_dir / "codex-model-picker.png",
                full_page=True,
                animations="disabled",
            )

            if catalog_state != "available":
                await expect(picker).to_have_attribute("aria-label", "Codex, Model unavailable")
                await expect(picker).to_contain_text("Models unavailable")
                await expect(models.get_by_role("menuitemcheckbox")).to_have_count(0)
                await expect(models).to_contain_text(
                    "Codex catalog probe failed"
                    if catalog_state == "failed"
                    else "Models unavailable"
                )
                return

            await expect(picker).to_have_attribute("aria-label", "Codex, Model Default")
            await expect(picker).to_contain_text("Default")
            await expect(picker).not_to_contain_text("Models unavailable")
            default = models.get_by_role("menuitemcheckbox", name="Harness default", exact=True)
            await expect(default).to_have_attribute("aria-checked", "true")
            await expect(models.get_by_role("menuitemcheckbox")).to_have_count(3)
            await models.get_by_role(
                "menuitemcheckbox", name="Catalog Model B", exact=True
            ).click()
            await expect(picker).to_have_attribute("aria-label", "Codex, Model Catalog Model B")
            if pinned_model is None:
                await default.click()
                await expect(picker).to_have_attribute("aria-label", "Codex, Model Default")
            await _close_entry_models(page)

            await page.get_by_test_id("new-chat-landing-input").fill("Inspect this repository")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(f"{base_url}/c/{session_id}")
            assert len(create_bodies) == 1
            assert create_bodies[0].get("model_override") == pinned_model
        finally:
            await context.close()
            await browser.close()
