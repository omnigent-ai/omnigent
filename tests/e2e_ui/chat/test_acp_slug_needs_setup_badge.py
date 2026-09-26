"""Verify configured ACP slugs do not show a setup warning."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e"
_HOST_NAME = "e2e-host"

_ACP_SLUG = "traex"
_ACP_HARNESS = f"acp:{_ACP_SLUG}"
_ACP_AGENT_ID = "ag_traex_e2e"
_ACP_DISPLAY_NAME = "TraeX"

_ACP_CONFIG_YAML = """\
acp:
  agents:
    - name: TraeX
      command: traex acp serve
"""


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run async browser code outside the suite's sync Playwright loop."""
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _real_configured_harness_map() -> dict[str, Any]:
    """Build the production readiness map from an isolated ACP config."""
    script = textwrap.dedent(
        """
        import json
        from omnigent.onboarding.harness_readiness import configured_harness_map
        print(json.dumps(configured_harness_map()))
        """
    )
    with tempfile.TemporaryDirectory() as home:
        (Path(home) / "config.yaml").write_text(_ACP_CONFIG_YAML)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "OMNIGENT_CONFIG_HOME": home},
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    return json.loads(proc.stdout)


def _hosts_body(configured_harnesses: dict[str, Any]) -> str:
    """Return one online host with the supplied readiness map."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": configured_harnesses,
                }
            ]
        }
    )


def _acp_agents_body() -> str:
    """Return the sole configured TraeX ACP agent."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _ACP_AGENT_ID,
                    "name": _ACP_SLUG,
                    "display_name": _ACP_DISPLAY_NAME,
                    "description": "Generic ACP agent",
                    "harness": _ACP_HARNESS,
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page: Any, *, configured_harnesses: dict[str, Any]) -> None:
    """Register deterministic host and agent responses."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_hosts_body(configured_harnesses),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_acp_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        # Keep unrelated sessions out of the picker.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(
        re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
    )


async def _open_landing(page: Any, base_url: str) -> None:
    """Open the landing composer with the ACP agent and target host selected."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )
    await page.add_init_script(
        f'window.localStorage.setItem("omnigent:last-agent-id", {json.dumps(_ACP_AGENT_ID)});'
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    host_chip = page.get_by_test_id("new-chat-landing-host-chip")
    await host_chip.click()
    host_row = page.get_by_test_id(f"new-chat-landing-host-{_HOST_ID}")
    await host_row.click(timeout=15_000)
    await expect(page.get_by_test_id("new-chat-landing-host-menu")).to_have_count(0)
    await host_chip.click()
    await expect(host_row).to_have_attribute("data-active", "true")
    await page.keyboard.press("Escape")
    await expect(page.get_by_test_id("new-chat-landing-host-menu")).to_have_count(0)
    await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
        "aria-label", re.compile(r"^Traex(?:,|$)", re.IGNORECASE)
    )


def test_configured_acp_slug_agent_is_not_badged_needs_setup(
    seeded_session: tuple[str, str],
) -> None:
    """Hide the warning for the real map and show it for a false control."""
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session -- only reads the picker

    real_map = _real_configured_harness_map()
    assert real_map, "production configured_harness_map() returned an empty map"
    assert real_map.get("acp"), (
        f"expected generic 'acp' readiness to be available with a configured agent, "
        f"got {real_map.get('acp')!r}"
    )
    assert real_map.get(_ACP_HARNESS) is True, (
        f"expected the configured {_ACP_HARNESS!r} slug key to be available in the "
        f"readiness map, got {real_map.get(_ACP_HARNESS)!r} -- the map no longer "
        "enumerates user-configured acp:<slug> agents"
    )

    _run_in_fresh_loop(_drive_acp_slug_badge(base_url, real_map))


async def _drive_acp_slug_badge(base_url: str, real_map: dict[str, Any]) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page, configured_harnesses=real_map)
            await _open_landing(page, base_url)

            await expect(page.get_by_test_id("new-chat-landing-harness-warning")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()

    control_map = {**real_map, _ACP_HARNESS: False}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page, configured_harnesses=control_map)
            await _open_landing(page, base_url)

            notice = page.get_by_test_id("new-chat-landing-harness-warning")
            await expect(notice).to_be_visible(timeout=30_000)
            await expect(notice).to_contain_text(_HOST_NAME)
        finally:
            await context.close()
            await browser.close()
