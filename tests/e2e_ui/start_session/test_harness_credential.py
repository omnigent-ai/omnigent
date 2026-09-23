"""E2E: needs-auth harnesses are disabled in the new-session picker.

Covers the picker contract where an installed but unauthenticated harness stays
visible and unselectable while its row-wide tooltip gives the login command.

Uses the same route-stubbing approach as ``test_harness_install.py``:
``/v1/info``, ``/v1/hosts``, and ``/v1/agents`` are faked so the test drives
the real UI without a live host or a real credential write.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_HOST_ID = "host_e2e"
# The stub host reports codex installed-but-not-configured; the credential POST
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
    """One online host; ``ready`` toggles codex between needs-auth and ready."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {_HARNESS: True if ready else "needs-auth"},
                }
            ]
        }
    )


def _info_body() -> str:
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
                        "title": "Set up authentication",
                        "detail": (
                            "Sign in with your ChatGPT subscription, an API key, or a gateway."
                        ),
                        "action": "auth",
                        "command": "codex login",
                        "status_key": "authed",
                    },
                ]
            },
        }
    )


async def _register_routes(page, *, credential_requests: list[dict[str, Any]]) -> None:
    """Stub info/hosts/agents/harnesses, the credential POST, and adopt-detect.

    The credential POST records its JSON body and returns a readiness map with
    the harness now ready, mirroring the real endpoint. Detect returns nothing
    (the test exercises the paste-key path, not adopt). ``/v1/hosts`` flips to
    ready once a credential has been written, so the warning clears.
    """

    async def handle_info(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_info_body())

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_hosts_body(ready=bool(credential_requests)),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_harnesses(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_harnesses_body())

    async def handle_detect(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"object": "detected_credentials", "credentials": []}),
        )

    async def handle_credential(route: Route) -> None:
        credential_requests.append(json.loads(route.request.post_data or "{}"))
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "harness_credential",
                    "harness": _HARNESS,
                    "configured_harnesses": {_HARNESS: True},
                }
            ),
        )

    async def handle_agent_scan(route: Route) -> None:
        # Exclude real agents left in the shared server so the stubbed Codex
        # stays selected and its setup notice remains visible.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/info", handle_info)
    await page.route("**/v1/hosts", handle_hosts)
    await stub_empty_host_picker_data(page, _HOST_ID)
    await page.route("**/v1/agents", handle_agents)
    await page.route(
        re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
    )
    await page.route("**/v1/harnesses", handle_harnesses)
    await page.route("**/v1/hosts/*/credentials/detected", handle_detect)
    await page.route(f"**/v1/hosts/*/harnesses/{_HARNESS}/credential", handle_credential)


async def _seed_workspace(page) -> None:
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


# ── Tests ──────────────────────────────────────────────────────────


def test_needs_auth_harness_is_disabled_with_repair_tooltip(
    live_server: str,
) -> None:
    """A needs-auth harness stays unselectable and explains how to authenticate."""
    base_url = live_server
    _run_in_fresh_loop(_drive_add_key(base_url))


async def _drive_add_key(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            credential_requests: list[dict[str, Any]] = []
            await _register_routes(page, credential_requests=credential_requests)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await picker.click()
            codex_option = page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e")
            await expect(codex_option).to_be_visible(timeout=60_000)
            await expect(codex_option).to_have_attribute("aria-disabled", "true")
            await codex_option.get_by_text("Codex", exact=True).hover()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-tooltip-ag_codex_e2e")
            ).to_contain_text(
                "Codex needs Codex authentication on e2e-host — run codex login on that machine."
            )
            assert credential_requests == []
        finally:
            await browser.close()
