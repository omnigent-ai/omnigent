"""E2E (hermetic): pre-launch model picker rows per model-flows-design.md §10.1.

Row 6's hermetic half: with a host catalog whose rows carry ``isDefault``, the
new-chat model select must read "Default (X)" for BOTH harnesses — X being the
default row's display name. Codex already renders this; the claude branch of
the landing screen historically discarded ``isDefault``, so its select read a
bare "Default" no matter what the host said. This test encodes the design's
target behavior and is red until landing-order step 7.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly like
the sibling tests in ``test_start_session.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _antigravity_native_agents_body,
    _close_entry_models,
    _codex_native_agents_body,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

_ANTIGRAVITY_HOST_ROWS = [
    {"id": "gemini-3.5-pro", "displayName": "Gemini 3.5 Pro", "isDefault": False},
    {"id": "claude-sonnet-4-6", "displayName": "Claude Sonnet 4.6", "isDefault": False},
    {"id": "gpt-oss-120b", "displayName": "GPT-OSS 120B", "isDefault": False},
]

_CLAUDE_HOST_ROWS = [
    {
        "id": "sonnet",
        "model": "claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": False,
    },
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": True,
    },
    {
        "id": "haiku",
        "model": "claude-haiku-4-5-20251001",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]


def test_claude_default_entry_names_the_true_default(
    seeded_session: tuple[str, str],
) -> None:
    """Row 6: the claude model select reads "Opus 4.8 (1M context)".

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_claude_default_label(base_url, session_id))


async def _drive_claude_default_label(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

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
                    body=json.dumps({"models": _CLAUDE_HOST_ROWS}),
                )

            import re as _re

            await page.route(_re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
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
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_entry_models(page, "ag_claude_e2e")
            model = page.get_by_test_id("new-chat-landing-agent-models")
            # The design's row 6: the untouched select names the model a
            # Default launch truly runs, for claude exactly as for codex.
            await expect(model).to_contain_text("Opus 4.8 (1M context)")
        finally:
            await browser.close()


def test_codex_probe_failure_shows_host_error(seeded_session: tuple[str, str]) -> None:
    """A failed host probe shows its structured error instead of the HTTP status line."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_codex_probe_failure(base_url, session_id))


async def _drive_codex_probe_failure(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            async def handle_model_options(route: Route) -> None:
                await route.fulfill(
                    status=502,
                    content_type="application/json",
                    body=json.dumps({"detail": "the codex model probe failed — see the host log"}),
                )

            import re as _re

            await page.route(_re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                handle_model_options,
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

            await expect(
                page.get_by_text("the codex model probe failed — see the host log", exact=True)
            ).to_be_visible(timeout=30_000)
        finally:
            await browser.close()


def test_antigravity_launch_picker_posts_selected_model(
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Antigravity's host catalog is selectable only before its terminal launches."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(
        _drive_antigravity_launch_picker(
            base_url,
            session_id,
            screenshot_path=tmp_path / "antigravity-launch-picker-fixture.png",
        )
    )


async def _drive_antigravity_launch_picker(
    base_url: str, session_id: str, *, screenshot_path: Path
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            agents = (
                json.loads(_agents_body())["data"]
                + json.loads(_antigravity_native_agents_body())["data"]
            )
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=json.dumps({"data": agents}),
            )

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
                    body=json.dumps({"models": _ANTIGRAVITY_HOST_ROWS}),
                )

            import re as _re

            await page.route(_re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/antigravity-native/model-options",
                handle_model_options,
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
            # Start on Claude, then use Antigravity's Edit row. This exercises
            # the catalog when its harness is not the landing default.
            assert (
                await page.get_by_test_id("new-chat-landing-agent-select").get_attribute(
                    "aria-label"
                )
            ).startswith("Claude Code,")
            await _open_entry_models(page, "ag_antigravity_e2e")
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_contain_text("Gemini 3.5 Pro")
            await expect(models).to_contain_text("Claude Sonnet 4.6")
            await expect(models).to_contain_text("GPT-OSS 120B")
            # This is an SPA fixture screenshot of the host-stubbed catalog,
            # not evidence that a local agy installation exposes these rows.
            await page.screenshot(path=str(screenshot_path), animations="disabled")

            await page.get_by_role(
                "menuitemcheckbox", name="Claude Sonnet 4.6", exact=True
            ).click()
            await expect(page.get_by_test_id("new-chat-landing-agent-efforts")).to_have_count(0)
            await _close_entry_models(page)
            await page.get_by_test_id("new-chat-landing-input").fill("inspect the repository")
            await page.get_by_test_id("new-chat-landing-submit").click()
            # The picker only pins model_override for the first launch. No
            # session PATCH or live model-change route is involved.
            from tests.e2e_ui.start_session.test_start_session import _wait_until

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_antigravity_e2e", body
            assert body.get("model_override") == "claude-sonnet-4-6", body
            assert "reasoning_effort" not in body, body
        finally:
            await browser.close()
