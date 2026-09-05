"""E2E: add a harness credential from the new-session setup dialog.

Covers the M3 journey where the selected agent's harness is installed but not
configured (``needs-auth``): the setup dialog offers "Set up auth", which expands an
inline credential form; pasting an API key POSTs the credential and the
readiness warning clears once the host reports the harness ready.

Uses the same route-stubbing approach as ``test_harness_install.py``:
``/v1/info``, ``/v1/hosts``, ``/v1/agents``, ``/v1/harnesses``, the credential
``POST`` and the adopt-detect ``GET`` are faked so the test drives the real UI
without a live host or a real credential write.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

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
        # The picker also scans GET /v1/sessions?kind=any for registered agents.
        # The seeded_session fixture creates real sessions in the DB, so without
        # this stub those leak in and the picker auto-selects the built-in Claude
        # Code (ready) instead of our unconfigured Codex — no "Set up" notice
        # (a CI-only failure). Return none so only the stubbed Codex populates it.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/info", handle_info)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
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


def test_add_key_configures_needs_auth_harness(
    seeded_session: tuple[str, str],
) -> None:
    """The setup dialog offers an inline credential form for a needs-auth
    harness; pasting a key writes it and clears the readiness warning."""
    base_url, _session_id = seeded_session
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

            # Commit the Codex agent (installed but needs-auth on the host). The
            # composer auto-selects the built-in Claude Code, not our stubbed
            # Codex, so select Codex explicitly — waiting for its row to render
            # (it mounts only after the /v1/agents fetch resolves; can lag on CI).
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            codex_option = page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e")
            await expect(codex_option).to_be_visible(timeout=60_000)
            await codex_option.click()

            # "Set up →" opens the dialog; there's no Install (already installed).
            setup = page.get_by_test_id("new-chat-landing-harness-setup")
            await expect(setup).to_be_visible(timeout=60_000)
            await setup.click()
            await expect(page.get_by_test_id("harness-setup-install")).to_have_count(0)

            # The auth step offers "Set up auth" → expands the inline credential form.
            await page.get_by_test_id("harness-setup-add-credential").click()
            await expect(page.get_by_test_id("harness-credential-form")).to_be_visible(
                timeout=5_000
            )

            # Paste a key and save → the credential POST is hit and the warning
            # clears once the host reports the harness ready.
            await page.get_by_test_id("harness-credential-key").fill("sk-ant-e2e-test")
            await page.get_by_test_id("harness-credential-save").click()
            await _wait_until(lambda: len(credential_requests) == 1)
            assert credential_requests[0]["kind"] == "key"
            assert credential_requests[0]["secret"] == "sk-ant-e2e-test"
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
