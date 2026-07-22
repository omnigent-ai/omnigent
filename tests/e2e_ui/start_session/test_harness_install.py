"""E2E: one-click harness install from the new-session landing page.

Covers the user journey where the selected agent's harness isn't set up on the
chosen host: the composer shows an "Install" button (instead of the "run
omnigent setup" hint), clicking it installs the harness, and the readiness
warning clears once the host reports the harness ready.

Uses the same route-stubbing approach as ``test_create_custom_agent.py``:
``/v1/info``, ``/v1/hosts``, ``/v1/agents`` and the install ``POST`` are faked
so the test drives the real UI without a live host or a real npm install.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e"
# The stub host reports codex's harness as not installed; the install POST
# flips it to ready so the warning clears.
_HARNESS = "codex-native"


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop."""
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


def _agents_body() -> str:
    """A single Codex agent whose harness the stub host lacks."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_codex_e2e",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": _HARNESS,
                    "skills": [],
                }
            ]
        }
    )


def _hosts_body(*, ready: bool) -> str:
    """One online host; ``ready`` toggles the harness's readiness."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {_HARNESS: ready},
                }
            ]
        }
    )


def _info_body() -> str:
    """``/v1/info`` with the install feature on and codex accepted."""
    return json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": False,
            "sandbox_provider": None,
            "sharing_mode": "on",
            "public_sharing_enabled": True,
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
            "harness_install_enabled": True,
            "installable_harnesses": ["codex", _HARNESS],
        }
    )


def _harnesses_body() -> str:
    """``/v1/harnesses`` with a setup_steps map keyed by the native spelling —
    the shape the setup dialog reads to render its checklist."""
    return json.dumps(
        {
            "data": [{"id": "codex", "label": "Codex"}],
            "setup_steps": {
                _HARNESS: [
                    {
                        "kind": "install",
                        "title": "Install Codex",
                        "detail": "We'll install Codex on the host for you.",
                        "action": "install",
                        "command": None,
                        "status_key": "installed",
                    },
                    {
                        "kind": "auth",
                        "title": "Sign in to Codex",
                        "detail": "Uses your ChatGPT subscription — sign in on the host.",
                        "action": "command",
                        "command": "codex login",
                        "status_key": "authed",
                    },
                ]
            },
        }
    )


async def _register_routes(page, *, install_requests: list[str]) -> None:
    """Stub info, hosts, agents, harnesses, and the harness-install POST.

    The install POST records the call and returns a readiness map with the
    harness now ready, mirroring the real endpoint's response.
    """

    async def handle_info(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_info_body())

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_hosts_body(ready=False)
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_harnesses(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_harnesses_body())

    async def handle_install(route: Route) -> None:
        install_requests.append(route.request.url)
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "harness_install",
                    "harness": _HARNESS,
                    "configured_harnesses": {_HARNESS: True},
                }
            ),
        )

    await page.route("**/v1/info", handle_info)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/harnesses", handle_harnesses)
    await page.route(f"**/v1/hosts/*/harnesses/{_HARNESS}/install", handle_install)


async def _seed_workspace(page) -> None:
    """Seed a recent workspace so the composer settles on the stub host."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


# ── Tests ──────────────────────────────────────────────────────────


def test_install_button_installs_missing_harness(
    seeded_session: tuple[str, str],
) -> None:
    """The composer offers Install for a missing harness; clicking it installs
    and clears the readiness warning."""
    base_url, _session_id = seeded_session
    _run_in_fresh_loop(_drive_install(base_url))


async def _drive_install(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            install_requests: list[str] = []
            await _register_routes(page, install_requests=install_requests)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Commit the Codex agent (its harness is unconfigured on the host).
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e").click()

            # The composer notice offers "Set up →", which opens the setup dialog.
            setup = page.get_by_test_id("new-chat-landing-harness-setup")
            await expect(setup).to_be_visible(timeout=10_000)
            await setup.click()

            # The dialog's checklist offers a one-click Install for this harness.
            install_button = page.get_by_test_id("harness-setup-install")
            await expect(install_button).to_be_visible(timeout=5_000)

            # Install → the endpoint is hit and the warning clears once the host
            # reports the harness ready (the response's readiness map is applied
            # to the cache, so no reconnect is needed).
            await install_button.click()
            await _wait_until(lambda: len(install_requests) == 1)
            await expect(page.get_by_test_id("new-chat-landing-harness-warning")).to_be_hidden(
                timeout=10_000
            )
        finally:
            await browser.close()


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")
