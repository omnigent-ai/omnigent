"""Browser regression: the web sends no keyless /v1 request or API WebSocket.

Two real server replicas share a database but not pub-sub, behind a keyed ingress
that records every request with its slice key. Across a cold boot, a reload and a
short steady state, no /v1 or /health request and no API WebSocket may leave the
browser without a key, since a keyless request lands on one hot replica.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    ManagedReplicas,
    _poll,
    managed_replicas,  # noqa: F401  (fixture)
)
from tests.e2e_ui.start_session.test_start_session import _run_in_fresh_loop

_SLICE_PARAM = "omnigent_slice_key"
_ROUTED_PATH = re.compile(r"/(v1/|health)")
_STEADY_MS = 10_000


@pytest.mark.timeout(300)
def test_no_request_is_keyless_on_boot_reload_or_steady_state(
    managed_replicas: ManagedReplicas,  # noqa: F811
) -> None:
    _run_in_fresh_loop(_drive(managed_replicas))


async def _count(client: httpx.AsyncClient, network_url: str) -> int:
    return len((await client.get(network_url)).json())


def _sessions_listed(rows: list[dict[str, Any]]) -> bool:
    return any(
        r["path"] == "/v1/sessions" and r["method"] == "GET" and r["status"] == 200 for r in rows
    )


async def _drive(rig: ManagedReplicas) -> None:
    network_url = f"{rig.ingress}/__test/network"
    sockets: list[str] = []
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 960})
        page = await context.new_page()
        page.on("websocket", lambda ws: sockets.append(ws.url))
        try:
            # The fixture's own readiness probes precede the browser.
            start = await _count(client, network_url)
            await page.goto(rig.ui)
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(
                timeout=60_000
            )
            await _poll(client, network_url, _sessions_listed, timeout=60)

            reload_start = await _count(client, network_url)
            await page.reload()
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(
                timeout=60_000
            )
            await _poll(client, network_url, _sessions_listed, timeout=60)

            steady_start = await _count(client, network_url)
            await page.wait_for_timeout(_STEADY_MS)

            rows = (await client.get(network_url)).json()
            browser_rows = rows[start:]
            routed = [r for r in browser_rows if _ROUTED_PATH.match(r["path"])]
            assert routed, "the browser sent no /v1 requests"
            keyless = [(r["method"], r["path"]) for r in routed if not r["key"]]
            assert not keyless, f"keyless requests: {keyless}"
            steady_keyless = [
                (r["method"], r["path"])
                for r in rows[steady_start:]
                if _ROUTED_PATH.match(r["path"]) and not r["key"]
            ]
            assert not steady_keyless, f"keyless steady-state requests: {steady_keyless}"
            assert len(rows) > reload_start, "the reload sent no requests"

            # Only sockets through the UI origin pass the sharding layer; direct
            # loopback sockets skip the ingress.
            ui_ws = rig.ui.replace("http://", "ws://")
            api_sockets = [url for url in sockets if url.startswith(f"{ui_ws}/v1/")]
            assert api_sockets, "the browser opened no API WebSocket"
            keyless_sockets = [url for url in api_sockets if _SLICE_PARAM not in url]
            assert not keyless_sockets, keyless_sockets
        finally:
            rows = (await client.get(network_url)).json()
            (rig.logs / "network.json").write_text(json.dumps(rows, indent=2))
            await page.screenshot(path=str(rig.logs / "final.png"))
            for url in (rig.a, rig.b):
                await client.post(f"{url}/__test/provision/release")
            await browser.close()
