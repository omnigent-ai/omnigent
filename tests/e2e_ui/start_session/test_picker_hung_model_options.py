"""New Chat must show the harness picker even when the host's model list never arrives."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.start_session.test_start_session import _HOST_ID, _agents_body, _hosts_body

# Twice the client's 30 s request deadline: a bounded load has settled by then.
_PICKER_DEADLINE_S = 60.0
_SAMPLE_INTERVAL_S = 5.0
_SCREENSHOT_AT_S = (0.0, 30.0, 60.0)
# The picker polls the catalog every 15 s; the next hung request must not hide it again.
_NEXT_POLL_TIMEOUT_S = 30.0


def test_picker_renders_when_model_options_hang(
    live_server: str, browser_name: str, tmp_path: Path
) -> None:
    """A hung model-options request must not hide the picker behind the spinner."""
    run_in_fresh_loop(_drive(live_server, browser_name, tmp_path))


async def _drive(base_url: str, browser_name: str, output: Path) -> None:
    async with async_playwright() as playwright:
        browser = await getattr(playwright, browser_name).launch()
        viewport = {"width": 1440, "height": 900}
        # The e2e conftest adds record_video_dir when OMNIGENT_E2E_RECORD_DIR is set.
        context = await browser.new_context(viewport=viewport, record_video_size=viewport)
        page = await context.new_page()
        release = asyncio.Event()
        models_requested = asyncio.Event()
        models_polled = asyncio.Event()
        try:

            async def agents(route: Route) -> None:
                await route.fulfill(content_type="application/json", body=_agents_body())

            async def hosts(route: Route) -> None:
                await route.fulfill(content_type="application/json", body=_hosts_body())

            async def models(route: Route) -> None:
                if models_requested.is_set():
                    models_polled.set()
                models_requested.set()
                # The host accepted the request but never answers it; by teardown the
                # client has already given up on it, so aborting may find nothing to abort.
                await release.wait()
                with contextlib.suppress(PlaywrightError):
                    await route.abort()

            async def worktrees(route: Route) -> None:
                await route.fulfill(
                    json={
                        "data": [
                            {
                                "path": "/work/repo",
                                "branch": "main",
                                "is_main": True,
                                "detached": False,
                            }
                        ]
                    }
                )

            async def info(route: Route) -> None:
                response = await route.fetch()
                body = await response.json()
                body.update(managed_sandboxes_enabled=False, smart_routing_enabled=False)
                await route.fulfill(response=response, json=body)

            await context.route(re.compile(r"/v1/agents(?:\?.*)?$"), agents)
            await context.route(re.compile(r"/v1/hosts(?:\?.*)?$"), hosts)
            await context.route("**/v1/hosts/*/harnesses/*/model-options", models)
            await context.route(re.compile(r"/v1/hosts/[^/]+/worktrees(?:\?.*)?$"), worktrees)
            await context.route("**/v1/info", info)
            await context.add_init_script(
                f"""localStorage.setItem('omnigent:last-agent-id', 'ag_claude_e2e');
                localStorage.setItem('omnigent:last-host-choice', '{_HOST_ID}');
                localStorage.setItem('omnigent:recent-workspaces',
                    JSON.stringify({{{_HOST_ID}: ['/work/repo']}}));"""
            )

            await page.goto(f"{base_url}/")
            loading = page.get_by_role("status", name="Loading session configuration")
            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(loading).to_be_visible(timeout=30_000)
            await asyncio.wait_for(models_requested.wait(), timeout=30)

            started = time.monotonic()
            samples: list[dict[str, object]] = []
            shots = list(_SCREENSHOT_AT_S)
            while True:
                elapsed = time.monotonic() - started
                sample = {
                    "t": round(elapsed, 1),
                    "spinner": await loading.count() > 0 and await loading.is_visible(),
                    "picker": await picker.count(),
                }
                samples.append(sample)
                while shots and elapsed >= shots[0]:
                    await page.screenshot(path=output / f"landing-{shots.pop(0):.0f}s.png")
                if sample["picker"] or elapsed >= _PICKER_DEADLINE_S:
                    break
                await asyncio.sleep(_SAMPLE_INTERVAL_S)
            (output / "picker-samples.json").write_text(json.dumps(samples, indent=1))

            assert samples[-1]["picker"], (
                f"harness picker never rendered within {_PICKER_DEADLINE_S:.0f}s while the "
                f"model-options request hung; samples: {samples}"
            )
            await expect(picker).to_be_visible()
            await expect(picker).to_be_enabled()
            await expect(loading).to_have_count(0)

            await asyncio.wait_for(models_polled.wait(), timeout=_NEXT_POLL_TIMEOUT_S)
            # Let React commit the poll's state change before checking the picker held.
            await page.evaluate(
                "() => new Promise(resolve => "
                "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            await expect(picker).to_be_visible()
            await expect(loading).to_have_count(0)
            await page.screenshot(path=output / "landing-picker-rendered.png")
        finally:
            release.set()
            await context.unroute_all(behavior="wait")
            await context.close()
            await browser.close()
